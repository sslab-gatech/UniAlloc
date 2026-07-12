//! Semantic allocation metadata and type-isolation compatibility hooks.
//!
//! The paper design extends allocation with compiler-visible object semantics.
//! This module provides the stable in-repository API for that path. The current
//! implementation records coverage and policy decisions, then routes through
//! the existing allocator fast path. Unmodified `GlobalAlloc` callers are
//! counted as fallback allocations unless an explicit scoped-metadata or
//! evaluation-only layout auto-metadata mode is active, so the coverage
//! denominator remains honest by default.

use core::alloc::Layout;
use core::mem::size_of;
use core::ptr::NonNull;
use core::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

use crate::cache::RustAllocator;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sys_alloc as system_alloc;
use spin::{Mutex, RwLock};

/// No semantic information was available for this allocation site.
pub const UNKNOWN_SEMANTIC_ID: u64 = 0;
const FNV1A_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV1A_PRIME: u64 = 0x0000_0100_0000_01b3;
const TYPE_CACHE_SLOTS: usize = 64;
const TYPE_CACHE_PROBE_LIMIT: usize = 4;
const MAX_TYPE_CACHE_DEPTH: usize = 64;
const SEGREGATED_TYPE_CACHE_DEPTH: usize = 8;
const SEGREGATED_TYPE_CACHE_PROBE_LIMIT: usize = 4;
const SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY: u8 = 0;
const SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE: u8 = 1;
const TYPE_CACHE_NODE_WORDS: usize = 3;
const DELAYED_FREE_SLOTS: usize = 32;
/// Maximum allocator-rounded bytes retained by one thread's delayed-free quarantine ring.
///
/// Delayed free is a reuse-hardening policy, but retaining 32 large objects can
/// pin multi-megabyte RSS per thread and bypass the allocator's coalescing/unmap
/// paths.  Keep a bounded quarantine for small/medium objects and release
/// over-budget objects through the ordinary deallocation path immediately.
pub const MAX_DELAYED_FREE_RETAINED_BYTES: usize = 256 * 1024;
/// Fast memory-tag records kept in TLS and in the cross-thread global table.
///
/// Memory tagging is a guarded/debug-style policy.  Keeping 1024 full
/// `TaggedAllocation` records in every thread and again in the global table was
/// a large fixed cost for a feature that already has correctness-preserving
/// overflow pages on hosted targets.  A 256-entry fast tier stays power-of-two
/// for cheap hashing and is currently 16KiB on 64-bit targets; overflow keeps
/// uncommon high-cardinality tag sets correct without reserving the whole side
/// table up front.
const MEMORY_TAG_FAST_SLOTS: usize = 256;
const MEMORY_TAG_FAST_PROBE_LIMIT: usize = 16;
/// Cross-thread memory-tag records are sharded to avoid one global lock.
///
/// The total fast-tier footprint intentionally stays the same as the TLS table:
/// 8 shards * 32 records = 256 records.  Hosted targets still have cold
/// overflow pages per shard when a bounded probe cannot place a record.
const GLOBAL_MEMORY_TAG_SHARD_COUNT: usize = 8;
const GLOBAL_MEMORY_TAG_SHARD_SLOTS: usize = MEMORY_TAG_FAST_SLOTS / GLOBAL_MEMORY_TAG_SHARD_COUNT;
const GLOBAL_MEMORY_TAG_SHARD_MASK: usize = GLOBAL_MEMORY_TAG_SHARD_COUNT - 1;
const GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT: usize = 16;
#[cfg(not(feature = "fixed_heap"))]
/// Cold overflow capacity for memory-tag records.
///
/// Overflow pages are allocated only after the bounded fast probe cannot place a
/// record.  128 entries keep the first spill page small (about 8KiB of records)
/// while still amortizing mmap cost for bursts.
const MEMORY_TAG_PAGE_SLOTS: usize = 128;
const AUTO_ALLOCATION_RECORD_SLOTS: usize = 4096;
const AUTO_ALLOCATION_RECORD_SHARD_COUNT: usize = 8;
const AUTO_ALLOCATION_RECORD_SHARD_SLOTS: usize =
    AUTO_ALLOCATION_RECORD_SLOTS / AUTO_ALLOCATION_RECORD_SHARD_COUNT;
const AUTO_ALLOCATION_RECORD_PROBE_LIMIT: usize = 16;
const AUTO_ALLOCATION_RECORD_SHARD_MASK: usize = AUTO_ALLOCATION_RECORD_SHARD_COUNT - 1;
const AUTO_ALLOCATION_RECORD_TOMBSTONE_PTR: usize = usize::MAX;
#[cfg(not(feature = "fixed_heap"))]
const AUTO_ALLOCATION_RECORD_OVERFLOW_SLOTS: usize = 512;
/// Same-thread compiler recovery is a TLS cache, not the full side table.
///
/// Keep it deliberately small (256 records, currently 16KiB on 64-bit targets)
/// so every thread does not reserve a large fixed recovery table just because
/// type-isolation support is compiled in.  When this bounded cache is full the
/// existing sharded global recovery table remains the correctness-preserving
/// spill path.
const FAST_AUTO_ALLOCATION_RECORD_SLOTS: usize = 256;
const FAST_AUTO_ALLOCATION_RECORD_PROBE_LIMIT: usize = 4;
const SEMANTIC_TYPE_STATS_SLOTS: usize = 512;
const SEMANTIC_TYPE_STATS_SHARD_COUNT: usize = 8;
const SEMANTIC_TYPE_STATS_SHARD_SLOTS: usize =
    SEMANTIC_TYPE_STATS_SLOTS / SEMANTIC_TYPE_STATS_SHARD_COUNT;
const SEMANTIC_SCOPE_STACK_CAPACITY: usize = 64;
const SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY: usize = 64;
const GUARD_PAGE_MIN_OBJECT_SIZE: usize = crate::PAGE_SIZE;
const SEGREGATED_TYPE_CACHE_POLICY_MASK: u32 = FLAG_METADATA_SEGREGATED
    | FLAG_HUGEPAGE_METADATA
    | FLAG_POINTER_AUTH
    | FLAG_METADATA_PROTECTION;
#[cfg(not(feature = "fixed_heap"))]
const HUGEPAGE_METADATA_MAPPING_GRANULARITY: usize = 2 * 1024 * 1024;

/// Minimum typed object size that can be cached by the type-isolated frontend.
pub const MIN_TYPE_CACHE_OBJECT_SIZE: usize = TYPE_CACHE_NODE_WORDS * size_of::<usize>();
/// Maximum object size retained by semantic type caches.
///
/// The semantic cache is a per-thread/per-type hot reuse tier.  Keeping very
/// large objects here can pin substantially more RSS than the ordinary allocator
/// thread-cache target, and it bypasses the slab/freelist coalescing policies
/// that handle large cold objects.  Match the thread-cache post-flush hot target
/// (64 KiB) so semantic reuse stays a hot-object optimization instead of a large
/// object retention mechanism.
pub const MAX_TYPE_CACHE_OBJECT_SIZE: usize = 64 * 1024;
/// Maximum cold linked-list bytes retained behind one plain semantic type slot.
///
/// `MAX_TYPE_CACHE_DEPTH` protects the number of cached objects, but a single
/// semantic type can otherwise retain 64 medium objects.  Keep the inline
/// one-object hot path, then cap the per-type linked overflow at the same 64 KiB
/// target as the ordinary thread cache.  The cap is enforced by a small per-slot
/// byte counter so typed deallocation stays O(1) instead of scanning the linked
/// cache on every push.
pub const MAX_TYPE_CACHE_SLOT_BYTES: usize = 64 * 1024;
/// Maximum allocator-rounded bytes retained by one thread's plain semantic cache.
///
/// Per-slot caps prevent one type/layout from retaining too many objects, but a
/// workload with many distinct semantic identities can otherwise keep every TLS
/// slot just below its own cap.  Keep the plain cache as a bounded hot reuse
/// tier across all slots; overflowed frees fall back to the ordinary allocator
/// deallocation path instead of being privately retained by one thread.
pub const MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES: usize = 512 * 1024;
/// Maximum allocator-rounded bytes retained by one metadata-segregated semantic cache bucket.
///
/// Side-table entries carry richer metadata and are stored in bounded buckets
/// instead of an in-object linked list.  Keep the same hot retained-byte target
/// as the plain semantic cache so one policy bucket cannot pin many medium
/// objects merely because its count is still below `SEGREGATED_TYPE_CACHE_DEPTH`.
pub const MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES: usize = 64 * 1024;
/// Maximum allocator-rounded bytes retained by one thread's metadata-segregated cache.
///
/// Bucket caps bound each policy/layout group, but many distinct semantic keys
/// can otherwise spread across the TLS bucket table.  Keep the side-table cache
/// as a bounded hot reuse tier just like the plain semantic cache.
pub const MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES: usize = 512 * 1024;
const TYPE_CACHE_NODE_ALIGN: usize = core::mem::align_of::<usize>();

#[inline]
fn allocator_retained_bytes_for_object(size: usize) -> usize {
    if size == 0 {
        0
    } else {
        crate::size_class::get_rounded_size(size)
    }
}

#[inline]
fn type_cache_retained_bytes_for_object(size: usize) -> usize {
    allocator_retained_bytes_for_object(size)
}

#[inline]
fn type_cache_retained_bytes_for_layout(layout: Layout) -> usize {
    type_cache_retained_bytes_for_object(layout.size())
}

#[inline]
fn delayed_free_retained_bytes_for_object(size: usize) -> usize {
    allocator_retained_bytes_for_object(size)
}

#[inline]
fn delayed_free_retained_bytes_for_layout(layout: Layout) -> usize {
    delayed_free_retained_bytes_for_object(layout.size())
}

/// Module id used by runtime layout auto-metadata when callers do not provide
/// a more specific module/context identifier.
pub const AUTO_LAYOUT_MODULE_ID: u64 = 0x554e_4941_5554_4f4d; // "UNIAUTOM"

/// Internal placement hint used to mark metadata synthesized from a `Layout`.
///
/// Layout-derived ids are useful for unmodified benchmark coverage accounting,
/// but they are not exact compiler-assigned type ids. Multiple unrelated Rust
/// types can share the same `(size, align)`, so this marker keeps them out of
/// exact type-cache reuse while still preserving typed coverage statistics.
const LAYOUT_DERIVED_PLACEMENT_HINT: u16 = 0xA770;

/// Compiler hint bit requesting thread-independent recovery metadata.
///
/// Rustc lowering can set this bit when escape analysis cannot prove that an
/// allocated heap object is freed on the allocating thread.  The allocator then
/// records the pointer-to-metadata entry in the process-visible recovery table
/// instead of the same-thread TLS fast table, preserving exact typed
/// deallocation for cross-thread drops without penalizing the default
/// single-thread compiler fast path.
pub const PLACEMENT_HINT_CROSS_THREAD_RECOVERY: u16 = 1 << 15;

/// Compiler-local scope hint: allocation and matching drop/realloc scopes are
/// both MIR-rewritten, so ordinary `GlobalAlloc` calls inside the scope do not
/// need a pointer-to-metadata recovery record.
///
/// Cross-thread recovery takes precedence if both bits are present; local
/// elision is only sound when the compiler keeps the object lifetime paired
/// inside the same thread/context.
pub const PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY: u16 = 1 << 14;

/// Route covered objects through type-isolated policy state.
pub const FLAG_TYPE_ISOLATED: u32 = 1 << 0;
/// Store allocator metadata out-of-line when the selected backend supports it.
pub const FLAG_METADATA_SEGREGATED: u32 = 1 << 1;
/// Prefer metadata placement backed by huge pages.
pub const FLAG_HUGEPAGE_METADATA: u32 = 1 << 2;
/// Zero or otherwise initialize memory on allocation/deallocation.
pub const FLAG_FORCE_INITIALIZE: u32 = 1 << 3;
/// Delay reuse of freed objects.
pub const FLAG_DELAYED_FREE: u32 = 1 << 4;
/// Enable guard-page placement for large/high-risk objects.
pub const FLAG_GUARD_PAGES: u32 = 1 << 5;
/// Hardware/software pointer authentication is desired for metadata pointers.
pub const FLAG_POINTER_AUTH: u32 = 1 << 6;
/// Hardware/software memory tagging is desired for this allocation.
pub const FLAG_MEMORY_TAGGING: u32 = 1 << 7;
/// Hardware/software metadata write protection is desired.
pub const FLAG_METADATA_PROTECTION: u32 = 1 << 8;

/// Compiler- or caller-provided allocation semantics.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct AllocationMetadata {
    /// Stable compiler-assigned object type id. Zero means unknown/fallback.
    pub type_id: u64,
    /// Optional module/crate/context id. Zero means unknown/fallback.
    pub module_id: u64,
    /// Policy flags requested for this allocation site.
    pub flags: u32,
    /// Optional lifetime/class hint for future placement policies.
    pub lifetime_hint: u16,
    /// Optional alignment/placement class hint for future backends.
    pub placement_hint: u16,
    /// Optional callsite hash for coverage/debug accounting.
    pub callsite: u64,
}

impl AllocationMetadata {
    pub const fn unknown() -> Self {
        Self {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            flags: 0,
            lifetime_hint: 0,
            placement_hint: 0,
            callsite: 0,
        }
    }

    pub const fn for_type(type_id: u64) -> Self {
        Self {
            type_id,
            module_id: UNKNOWN_SEMANTIC_ID,
            flags: FLAG_TYPE_ISOLATED,
            lifetime_hint: 0,
            placement_hint: 0,
            callsite: 0,
        }
    }

    pub fn for_rust_type<T>() -> Self {
        Self::for_type(semantic_type_id::<T>())
    }

    pub const fn with_flags(mut self, flags: u32) -> Self {
        self.flags = flags;
        self
    }

    pub const fn with_module(mut self, module_id: u64) -> Self {
        self.module_id = module_id;
        self
    }

    pub const fn with_callsite(mut self, callsite: u64) -> Self {
        self.callsite = callsite;
        self
    }

    pub const fn with_lifetime_hint(mut self, lifetime_hint: u16) -> Self {
        self.lifetime_hint = lifetime_hint;
        self
    }

    pub const fn with_placement_hint(mut self, placement_hint: u16) -> Self {
        self.placement_hint = placement_hint;
        self
    }

    pub const fn has_type(self) -> bool {
        self.type_id != UNKNOWN_SEMANTIC_ID
    }

    pub const fn requests(self, flag: u32) -> bool {
        (self.flags & flag) != 0
    }

    pub const fn is_layout_derived(self) -> bool {
        self.placement_hint == LAYOUT_DERIVED_PLACEMENT_HINT
    }
}

#[inline]
fn fnv1a_mix(mut hash: u64, bytes: &[u8]) -> u64 {
    for byte in bytes {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(FNV1A_PRIME);
    }
    hash
}

#[inline]
fn non_zero_hash(hash: u64) -> u64 {
    if hash == UNKNOWN_SEMANTIC_ID {
        FNV1A_OFFSET
    } else {
        hash
    }
}

#[inline]
fn mix_semantic_metadata_fields(
    hash: u64,
    metadata: AllocationMetadata,
    include_callsite: bool,
) -> u64 {
    let hash = fnv1a_mix(hash, &metadata.type_id.to_le_bytes());
    let hash = fnv1a_mix(hash, &metadata.module_id.to_le_bytes());
    let hash = fnv1a_mix(hash, &metadata.flags.to_le_bytes());
    let hash = fnv1a_mix(hash, &metadata.lifetime_hint.to_le_bytes());
    let hash = fnv1a_mix(hash, &metadata.placement_hint.to_le_bytes());
    if include_callsite {
        fnv1a_mix(hash, &metadata.callsite.to_le_bytes())
    } else {
        hash
    }
}

/// Deterministically derive a non-zero semantic id from Rust type metadata.
///
/// The paper prototype assigns type IDs in the compiler. This helper is not a
/// replacement for that pass, but it gives in-tree manual/ABI instrumentation a
/// stable type-derived identifier so coverage experiments do not rely on
/// hard-coded constants.
pub fn semantic_type_id<T>() -> u64 {
    let hash = fnv1a_mix(FNV1A_OFFSET, b"rust-type-v1");
    let hash = fnv1a_mix(hash, core::any::type_name::<T>().as_bytes());
    let hash = fnv1a_mix(hash, &core::mem::size_of::<T>().to_le_bytes());
    let hash = fnv1a_mix(hash, &core::mem::align_of::<T>().to_le_bytes());
    non_zero_hash(hash)
}

/// Deterministically derive a non-zero semantic id from allocation layout.
///
/// This is intentionally an evaluation/runtime compatibility helper, not a
/// replacement for compiler-assigned type metadata. Many Rust types share a
/// `(size, align)` pair, so layout IDs are useful for unmodified benchmark smoke
/// coverage and upper-bound accounting only. Claim-grade C002 evidence must
/// still come from compiler/runtime type metadata.
pub fn semantic_layout_id(size: usize, align: usize) -> u64 {
    let hash = fnv1a_mix(FNV1A_OFFSET, b"layout-auto-v1");
    let hash = fnv1a_mix(hash, &size.to_le_bytes());
    let hash = fnv1a_mix(hash, &align.to_le_bytes());
    non_zero_hash(hash)
}

impl Default for AllocationMetadata {
    fn default() -> Self {
        Self::unknown()
    }
}

/// Atomic counters used by evaluation coverage checks.
pub struct SemanticStats {
    total_allocations: AtomicUsize,
    typed_allocations: AtomicUsize,
    fallback_allocations: AtomicUsize,
    total_allocated_bytes: AtomicUsize,
    typed_allocated_bytes: AtomicUsize,
    fallback_allocated_bytes: AtomicUsize,
    total_deallocations: AtomicUsize,
    typed_deallocations: AtomicUsize,
    fallback_deallocations: AtomicUsize,
    policy_flags_seen: AtomicUsize,
    last_type_id: AtomicU64,
    typed_cache_hits: AtomicUsize,
    typed_cache_inserts: AtomicUsize,
    typed_cache_bypasses: AtomicUsize,
    delayed_free_enqueues: AtomicUsize,
    delayed_free_flushes: AtomicUsize,
    metadata_pac_auth_signs: AtomicUsize,
    metadata_pac_auth_verifications: AtomicUsize,
    metadata_pac_auth_failures: AtomicUsize,
    metadata_pac_software_fallback_signs: AtomicUsize,
    metadata_pac_software_fallback_verifications: AtomicUsize,
    metadata_pac_software_fallback_failures: AtomicUsize,
    semantic_type_stats_dropped_events: AtomicUsize,
}

/// Atomic reason counters for fallback GlobalAlloc traffic.
///
/// `SemanticStats` intentionally keeps the public aggregate ABI small and
/// stable.  These companion counters make the honest fallback denominator
/// actionable: coverage probes can distinguish truly untyped allocation sites
/// from reallocations that recovered old-object metadata but still lacked
/// compiler metadata for the new object.
pub struct SemanticFallbackAttribution {
    raw_alloc_no_metadata: AtomicUsize,
    raw_alloc_no_metadata_bytes: AtomicUsize,
    raw_dealloc_no_metadata: AtomicUsize,
    raw_realloc_no_metadata: AtomicUsize,
    raw_realloc_no_metadata_bytes: AtomicUsize,
    raw_realloc_moved_dealloc_no_metadata: AtomicUsize,
    realloc_recorded_old_metadata_new_allocations: AtomicUsize,
    realloc_recorded_old_metadata_new_allocation_bytes: AtomicUsize,
}

/// Atomic validation counters for compiler/runtime metadata recovery.
///
/// Type isolation must not silently trust a deallocation/drop-site metadata row
/// when the allocator already recorded a different allocation identity for the
/// pointer.  The runtime still releases the object under the recorded
/// allocation identity (to avoid poisoning the wrong type cache), but these
/// counters make compiler identity mismatches visible to real probes.
pub struct SemanticMetadataValidation {
    recovery_identity_matches: AtomicUsize,
    recovery_identity_mismatches: AtomicUsize,
    last_mismatch_requested_type_id: AtomicU64,
    last_mismatch_recorded_type_id: AtomicU64,
    last_mismatch_requested_module_id: AtomicU64,
    last_mismatch_recorded_module_id: AtomicU64,
    last_mismatch_requested_callsite: AtomicU64,
    last_mismatch_recorded_callsite: AtomicU64,
}

/// Version for the exported semantic-stats snapshot ABI.
///
/// The snapshot is an evaluation/runtime integration structure and may grow as
/// the claim-grade evidence contract gains counters.  Size-negotiated FFI
/// callers should check this version plus `__unialloc_semantic_stats_snapshot_size`.
pub const SEMANTIC_STATS_SNAPSHOT_ABI_VERSION: u32 = 3;

/// Version for the exported fallback-attribution snapshot ABI.
pub const SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION: u32 = 1;

/// Version for the exported metadata-validation snapshot ABI.
pub const SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION: u32 = 1;

/// Version for the exported per-type semantic-stats row ABI.
///
/// External integrations copy an array of `SemanticTypeStatsSnapshot` rows, so
/// they must negotiate both this version and the row size before reading rows
/// produced by a potentially newer runtime.
pub const SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION: u32 = 2;

impl SemanticStats {
    pub const fn new() -> Self {
        Self {
            total_allocations: AtomicUsize::new(0),
            typed_allocations: AtomicUsize::new(0),
            fallback_allocations: AtomicUsize::new(0),
            total_allocated_bytes: AtomicUsize::new(0),
            typed_allocated_bytes: AtomicUsize::new(0),
            fallback_allocated_bytes: AtomicUsize::new(0),
            total_deallocations: AtomicUsize::new(0),
            typed_deallocations: AtomicUsize::new(0),
            fallback_deallocations: AtomicUsize::new(0),
            policy_flags_seen: AtomicUsize::new(0),
            last_type_id: AtomicU64::new(UNKNOWN_SEMANTIC_ID),
            typed_cache_hits: AtomicUsize::new(0),
            typed_cache_inserts: AtomicUsize::new(0),
            typed_cache_bypasses: AtomicUsize::new(0),
            delayed_free_enqueues: AtomicUsize::new(0),
            delayed_free_flushes: AtomicUsize::new(0),
            metadata_pac_auth_signs: AtomicUsize::new(0),
            metadata_pac_auth_verifications: AtomicUsize::new(0),
            metadata_pac_auth_failures: AtomicUsize::new(0),
            metadata_pac_software_fallback_signs: AtomicUsize::new(0),
            metadata_pac_software_fallback_verifications: AtomicUsize::new(0),
            metadata_pac_software_fallback_failures: AtomicUsize::new(0),
            semantic_type_stats_dropped_events: AtomicUsize::new(0),
        }
    }

    #[inline]
    fn record_policy_flags(&self, metadata: AllocationMetadata) {
        self.policy_flags_seen
            .fetch_or(metadata.flags as usize, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_alloc(&self, metadata: AllocationMetadata, bytes: usize) {
        self.total_allocations.fetch_add(1, Ordering::Relaxed);
        self.total_allocated_bytes
            .fetch_add(bytes, Ordering::Relaxed);
        self.record_policy_flags(metadata);
        if metadata.has_type() {
            self.typed_allocations.fetch_add(1, Ordering::Relaxed);
            self.typed_allocated_bytes
                .fetch_add(bytes, Ordering::Relaxed);
            self.last_type_id.store(metadata.type_id, Ordering::Relaxed);
        } else {
            self.fallback_allocations.fetch_add(1, Ordering::Relaxed);
            self.fallback_allocated_bytes
                .fetch_add(bytes, Ordering::Relaxed);
        }
    }

    #[inline]
    pub fn record_dealloc(&self, metadata: AllocationMetadata) {
        self.total_deallocations.fetch_add(1, Ordering::Relaxed);
        self.record_policy_flags(metadata);
        if metadata.has_type() {
            self.typed_deallocations.fetch_add(1, Ordering::Relaxed);
        } else {
            self.fallback_deallocations.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[inline]
    pub fn record_type_cache_hit(&self) {
        self.typed_cache_hits.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_type_cache_insert(&self) {
        self.typed_cache_inserts.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_type_cache_bypass(&self) {
        self.typed_cache_bypasses.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_delayed_free_enqueue(&self) {
        self.delayed_free_enqueues.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_delayed_free_flush(&self) {
        self.delayed_free_flushes.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_metadata_pac_auth_sign(&self) {
        self.metadata_pac_auth_signs.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_metadata_pac_auth_verification(&self, valid: bool) {
        self.metadata_pac_auth_verifications
            .fetch_add(1, Ordering::Relaxed);
        if !valid {
            self.metadata_pac_auth_failures
                .fetch_add(1, Ordering::Relaxed);
        }
    }

    #[inline]
    pub fn record_metadata_pac_software_fallback_sign(&self) {
        self.metadata_pac_software_fallback_signs
            .fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_metadata_pac_software_fallback_verification(&self, valid: bool) {
        self.metadata_pac_software_fallback_verifications
            .fetch_add(1, Ordering::Relaxed);
        if !valid {
            self.metadata_pac_software_fallback_failures
                .fetch_add(1, Ordering::Relaxed);
        }
    }

    #[inline]
    fn record_semantic_type_stats_drop(&self) {
        self.semantic_type_stats_dropped_events
            .fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    fn reset_semantic_type_stats_drops(&self) {
        self.semantic_type_stats_dropped_events
            .store(0, Ordering::Relaxed);
    }

    pub fn reset(&self) {
        self.total_allocations.store(0, Ordering::Relaxed);
        self.typed_allocations.store(0, Ordering::Relaxed);
        self.fallback_allocations.store(0, Ordering::Relaxed);
        self.total_allocated_bytes.store(0, Ordering::Relaxed);
        self.typed_allocated_bytes.store(0, Ordering::Relaxed);
        self.fallback_allocated_bytes.store(0, Ordering::Relaxed);
        self.total_deallocations.store(0, Ordering::Relaxed);
        self.typed_deallocations.store(0, Ordering::Relaxed);
        self.fallback_deallocations.store(0, Ordering::Relaxed);
        self.policy_flags_seen.store(0, Ordering::Relaxed);
        self.last_type_id
            .store(UNKNOWN_SEMANTIC_ID, Ordering::Relaxed);
        self.typed_cache_hits.store(0, Ordering::Relaxed);
        self.typed_cache_inserts.store(0, Ordering::Relaxed);
        self.typed_cache_bypasses.store(0, Ordering::Relaxed);
        self.delayed_free_enqueues.store(0, Ordering::Relaxed);
        self.delayed_free_flushes.store(0, Ordering::Relaxed);
        self.metadata_pac_auth_signs.store(0, Ordering::Relaxed);
        self.metadata_pac_auth_verifications
            .store(0, Ordering::Relaxed);
        self.metadata_pac_auth_failures.store(0, Ordering::Relaxed);
        self.metadata_pac_software_fallback_signs
            .store(0, Ordering::Relaxed);
        self.metadata_pac_software_fallback_verifications
            .store(0, Ordering::Relaxed);
        self.metadata_pac_software_fallback_failures
            .store(0, Ordering::Relaxed);
        self.reset_semantic_type_stats_drops();
    }

    pub fn snapshot(&self) -> SemanticStatsSnapshot {
        let total = self.total_allocations.load(Ordering::Relaxed);
        let typed = self.typed_allocations.load(Ordering::Relaxed);
        SemanticStatsSnapshot {
            total_allocations: total,
            typed_allocations: typed,
            fallback_allocations: self.fallback_allocations.load(Ordering::Relaxed),
            total_allocated_bytes: self.total_allocated_bytes.load(Ordering::Relaxed),
            typed_allocated_bytes: self.typed_allocated_bytes.load(Ordering::Relaxed),
            fallback_allocated_bytes: self.fallback_allocated_bytes.load(Ordering::Relaxed),
            typed_deallocations: self.typed_deallocations.load(Ordering::Relaxed),
            fallback_deallocations: self.fallback_deallocations.load(Ordering::Relaxed),
            policy_flags_seen: self.policy_flags_seen.load(Ordering::Relaxed) as u32,
            last_type_id: self.last_type_id.load(Ordering::Relaxed),
            coverage_basis_points: if total == 0 {
                0
            } else {
                typed * 10_000 / total
            },
            typed_cache_hits: self.typed_cache_hits.load(Ordering::Relaxed),
            typed_cache_inserts: self.typed_cache_inserts.load(Ordering::Relaxed),
            typed_cache_bypasses: self.typed_cache_bypasses.load(Ordering::Relaxed),
            delayed_free_enqueues: self.delayed_free_enqueues.load(Ordering::Relaxed),
            delayed_free_flushes: self.delayed_free_flushes.load(Ordering::Relaxed),
            metadata_pac_auth_signs: self.metadata_pac_auth_signs.load(Ordering::Relaxed),
            metadata_pac_auth_verifications: self
                .metadata_pac_auth_verifications
                .load(Ordering::Relaxed),
            metadata_pac_auth_failures: self.metadata_pac_auth_failures.load(Ordering::Relaxed),
            metadata_pac_software_fallback_signs: self
                .metadata_pac_software_fallback_signs
                .load(Ordering::Relaxed),
            metadata_pac_software_fallback_verifications: self
                .metadata_pac_software_fallback_verifications
                .load(Ordering::Relaxed),
            metadata_pac_software_fallback_failures: self
                .metadata_pac_software_fallback_failures
                .load(Ordering::Relaxed),
            total_deallocations: self.total_deallocations.load(Ordering::Relaxed),
            semantic_type_stats_dropped_events: self
                .semantic_type_stats_dropped_events
                .load(Ordering::Relaxed),
        }
    }
}

impl Default for SemanticStats {
    fn default() -> Self {
        Self::new()
    }
}

impl SemanticFallbackAttribution {
    pub const fn new() -> Self {
        Self {
            raw_alloc_no_metadata: AtomicUsize::new(0),
            raw_alloc_no_metadata_bytes: AtomicUsize::new(0),
            raw_dealloc_no_metadata: AtomicUsize::new(0),
            raw_realloc_no_metadata: AtomicUsize::new(0),
            raw_realloc_no_metadata_bytes: AtomicUsize::new(0),
            raw_realloc_moved_dealloc_no_metadata: AtomicUsize::new(0),
            realloc_recorded_old_metadata_new_allocations: AtomicUsize::new(0),
            realloc_recorded_old_metadata_new_allocation_bytes: AtomicUsize::new(0),
        }
    }

    #[inline]
    pub fn record_raw_alloc_no_metadata(&self, bytes: usize) {
        self.raw_alloc_no_metadata.fetch_add(1, Ordering::Relaxed);
        self.raw_alloc_no_metadata_bytes
            .fetch_add(bytes, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_raw_dealloc_no_metadata(&self) {
        self.raw_dealloc_no_metadata.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_raw_realloc_no_metadata(&self, bytes: usize) {
        self.raw_realloc_no_metadata.fetch_add(1, Ordering::Relaxed);
        self.raw_realloc_no_metadata_bytes
            .fetch_add(bytes, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_raw_realloc_moved_dealloc_no_metadata(&self) {
        self.raw_realloc_moved_dealloc_no_metadata
            .fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_realloc_recorded_old_metadata_new_allocation(&self, bytes: usize) {
        self.realloc_recorded_old_metadata_new_allocations
            .fetch_add(1, Ordering::Relaxed);
        self.realloc_recorded_old_metadata_new_allocation_bytes
            .fetch_add(bytes, Ordering::Relaxed);
    }

    pub fn reset(&self) {
        self.raw_alloc_no_metadata.store(0, Ordering::Relaxed);
        self.raw_alloc_no_metadata_bytes.store(0, Ordering::Relaxed);
        self.raw_dealloc_no_metadata.store(0, Ordering::Relaxed);
        self.raw_realloc_no_metadata.store(0, Ordering::Relaxed);
        self.raw_realloc_no_metadata_bytes
            .store(0, Ordering::Relaxed);
        self.raw_realloc_moved_dealloc_no_metadata
            .store(0, Ordering::Relaxed);
        self.realloc_recorded_old_metadata_new_allocations
            .store(0, Ordering::Relaxed);
        self.realloc_recorded_old_metadata_new_allocation_bytes
            .store(0, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> SemanticFallbackAttributionSnapshot {
        SemanticFallbackAttributionSnapshot {
            raw_alloc_no_metadata: self.raw_alloc_no_metadata.load(Ordering::Relaxed),
            raw_alloc_no_metadata_bytes: self.raw_alloc_no_metadata_bytes.load(Ordering::Relaxed),
            raw_dealloc_no_metadata: self.raw_dealloc_no_metadata.load(Ordering::Relaxed),
            raw_realloc_no_metadata: self.raw_realloc_no_metadata.load(Ordering::Relaxed),
            raw_realloc_no_metadata_bytes: self
                .raw_realloc_no_metadata_bytes
                .load(Ordering::Relaxed),
            raw_realloc_moved_dealloc_no_metadata: self
                .raw_realloc_moved_dealloc_no_metadata
                .load(Ordering::Relaxed),
            realloc_recorded_old_metadata_new_allocations: self
                .realloc_recorded_old_metadata_new_allocations
                .load(Ordering::Relaxed),
            realloc_recorded_old_metadata_new_allocation_bytes: self
                .realloc_recorded_old_metadata_new_allocation_bytes
                .load(Ordering::Relaxed),
        }
    }
}

impl Default for SemanticFallbackAttribution {
    fn default() -> Self {
        Self::new()
    }
}

impl SemanticMetadataValidation {
    pub const fn new() -> Self {
        Self {
            recovery_identity_matches: AtomicUsize::new(0),
            recovery_identity_mismatches: AtomicUsize::new(0),
            last_mismatch_requested_type_id: AtomicU64::new(UNKNOWN_SEMANTIC_ID),
            last_mismatch_recorded_type_id: AtomicU64::new(UNKNOWN_SEMANTIC_ID),
            last_mismatch_requested_module_id: AtomicU64::new(UNKNOWN_SEMANTIC_ID),
            last_mismatch_recorded_module_id: AtomicU64::new(UNKNOWN_SEMANTIC_ID),
            last_mismatch_requested_callsite: AtomicU64::new(0),
            last_mismatch_recorded_callsite: AtomicU64::new(0),
        }
    }

    #[inline]
    pub fn record_recovery_identity_match(&self) {
        self.recovery_identity_matches
            .fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn record_recovery_identity_mismatch(
        &self,
        requested: AllocationMetadata,
        recorded: AllocationMetadata,
    ) {
        self.recovery_identity_mismatches
            .fetch_add(1, Ordering::Relaxed);
        self.last_mismatch_requested_type_id
            .store(requested.type_id, Ordering::Relaxed);
        self.last_mismatch_recorded_type_id
            .store(recorded.type_id, Ordering::Relaxed);
        self.last_mismatch_requested_module_id
            .store(requested.module_id, Ordering::Relaxed);
        self.last_mismatch_recorded_module_id
            .store(recorded.module_id, Ordering::Relaxed);
        self.last_mismatch_requested_callsite
            .store(requested.callsite, Ordering::Relaxed);
        self.last_mismatch_recorded_callsite
            .store(recorded.callsite, Ordering::Relaxed);
    }

    pub fn reset(&self) {
        self.recovery_identity_matches.store(0, Ordering::Relaxed);
        self.recovery_identity_mismatches
            .store(0, Ordering::Relaxed);
        self.last_mismatch_requested_type_id
            .store(UNKNOWN_SEMANTIC_ID, Ordering::Relaxed);
        self.last_mismatch_recorded_type_id
            .store(UNKNOWN_SEMANTIC_ID, Ordering::Relaxed);
        self.last_mismatch_requested_module_id
            .store(UNKNOWN_SEMANTIC_ID, Ordering::Relaxed);
        self.last_mismatch_recorded_module_id
            .store(UNKNOWN_SEMANTIC_ID, Ordering::Relaxed);
        self.last_mismatch_requested_callsite
            .store(0, Ordering::Relaxed);
        self.last_mismatch_recorded_callsite
            .store(0, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> SemanticMetadataValidationSnapshot {
        SemanticMetadataValidationSnapshot {
            recovery_identity_matches: self.recovery_identity_matches.load(Ordering::Relaxed),
            recovery_identity_mismatches: self.recovery_identity_mismatches.load(Ordering::Relaxed),
            last_mismatch_requested_type_id: self
                .last_mismatch_requested_type_id
                .load(Ordering::Relaxed),
            last_mismatch_recorded_type_id: self
                .last_mismatch_recorded_type_id
                .load(Ordering::Relaxed),
            last_mismatch_requested_module_id: self
                .last_mismatch_requested_module_id
                .load(Ordering::Relaxed),
            last_mismatch_recorded_module_id: self
                .last_mismatch_recorded_module_id
                .load(Ordering::Relaxed),
            last_mismatch_requested_callsite: self
                .last_mismatch_requested_callsite
                .load(Ordering::Relaxed),
            last_mismatch_recorded_callsite: self
                .last_mismatch_recorded_callsite
                .load(Ordering::Relaxed),
        }
    }
}

impl Default for SemanticMetadataValidation {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct SemanticStatsSnapshot {
    pub total_allocations: usize,
    pub typed_allocations: usize,
    pub fallback_allocations: usize,
    pub total_allocated_bytes: usize,
    pub typed_allocated_bytes: usize,
    pub fallback_allocated_bytes: usize,
    pub typed_deallocations: usize,
    pub fallback_deallocations: usize,
    pub policy_flags_seen: u32,
    pub last_type_id: u64,
    /// Typed allocation coverage in basis points, e.g. 7217 = 72.17%.
    pub coverage_basis_points: usize,
    pub typed_cache_hits: usize,
    pub typed_cache_inserts: usize,
    pub typed_cache_bypasses: usize,
    pub delayed_free_enqueues: usize,
    pub delayed_free_flushes: usize,
    pub metadata_pac_auth_signs: usize,
    pub metadata_pac_auth_verifications: usize,
    pub metadata_pac_auth_failures: usize,
    pub metadata_pac_software_fallback_signs: usize,
    pub metadata_pac_software_fallback_verifications: usize,
    pub metadata_pac_software_fallback_failures: usize,
    /// Independent total deallocation events recorded before typed/fallback
    /// classification.  Runtime evaluators use this to reject self-derived
    /// deallocation totals.
    pub total_deallocations: usize,
    /// Per-type accounting events discarded because the fixed semantic stats
    /// table was full. Claim-grade consumers must require this to be zero.
    pub semantic_type_stats_dropped_events: usize,
}

impl SemanticStatsSnapshot {
    pub const fn coverage_percent(self) -> (usize, usize) {
        (
            self.coverage_basis_points / 100,
            self.coverage_basis_points % 100,
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct SemanticFallbackAttributionSnapshot {
    pub raw_alloc_no_metadata: usize,
    pub raw_alloc_no_metadata_bytes: usize,
    pub raw_dealloc_no_metadata: usize,
    pub raw_realloc_no_metadata: usize,
    pub raw_realloc_no_metadata_bytes: usize,
    pub raw_realloc_moved_dealloc_no_metadata: usize,
    pub realloc_recorded_old_metadata_new_allocations: usize,
    pub realloc_recorded_old_metadata_new_allocation_bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct SemanticMetadataValidationSnapshot {
    pub recovery_identity_matches: usize,
    pub recovery_identity_mismatches: usize,
    pub last_mismatch_requested_type_id: u64,
    pub last_mismatch_recorded_type_id: u64,
    pub last_mismatch_requested_module_id: u64,
    pub last_mismatch_recorded_module_id: u64,
    pub last_mismatch_requested_callsite: u64,
    pub last_mismatch_recorded_callsite: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct SemanticScopeDepthSnapshot {
    pub main_depth: usize,
    pub overflow_depth: usize,
    pub overflow_capacity: usize,
    pub represented_depth: usize,
    pub active_overflow_scope_represented: bool,
}

pub static SEMANTIC_STATS: SemanticStats = SemanticStats::new();
pub static SEMANTIC_FALLBACK_ATTRIBUTION: SemanticFallbackAttribution =
    SemanticFallbackAttribution::new();
pub static SEMANTIC_METADATA_VALIDATION: SemanticMetadataValidation =
    SemanticMetadataValidation::new();
#[cfg(test)]
static SEMANTIC_TEST_LOCK: Mutex<()> = Mutex::new(());
#[cfg(test)]
static SEMANTIC_STATS_TEST_EXACT_RECORDING_OWNERS: AtomicUsize = AtomicUsize::new(0);

#[cfg(test)]
#[thread_local]
static mut SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH: usize = 0;

#[cfg(test)]
pub(crate) struct SemanticTestGuard {
    #[cfg(feature = "fixed_heap")]
    _fixed_heap: spin::MutexGuard<'static, ()>,
    _semantic: spin::MutexGuard<'static, ()>,
}

#[cfg(test)]
pub(crate) fn semantic_test_guard() -> SemanticTestGuard {
    #[cfg(feature = "fixed_heap")]
    let fixed_heap = crate::sc::fixed_heap_test_guard();
    let semantic = SEMANTIC_TEST_LOCK.lock();
    SemanticTestGuard {
        #[cfg(feature = "fixed_heap")]
        _fixed_heap: fixed_heap,
        _semantic: semantic,
    }
}

#[cfg(test)]
pub(crate) fn semantic_stats_test_exact_recording_enter() {
    unsafe {
        if SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH == 0 {
            SEMANTIC_STATS_TEST_EXACT_RECORDING_OWNERS.fetch_add(1, Ordering::SeqCst);
        }
        SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH =
            SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH.saturating_add(1);
    }
}

#[cfg(test)]
pub(crate) fn semantic_stats_test_exact_recording_exit() {
    unsafe {
        debug_assert!(
            SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH != 0,
            "semantic stats exact-recording scope underflow"
        );
        if SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH == 0 {
            return;
        }
        SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH -= 1;
        if SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH == 0 {
            SEMANTIC_STATS_TEST_EXACT_RECORDING_OWNERS.fetch_sub(1, Ordering::SeqCst);
        }
    }
}

#[cfg(test)]
#[inline]
fn semantic_stats_test_exact_recording_allows_current_thread() -> bool {
    SEMANTIC_STATS_TEST_EXACT_RECORDING_OWNERS.load(Ordering::Acquire) == 0
        || unsafe { SEMANTIC_STATS_TEST_EXACT_RECORDING_DEPTH != 0 }
}

static SEMANTIC_SLOW_PATH_FLAGS: AtomicUsize = AtomicUsize::new(0);
static FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT: AtomicUsize = AtomicUsize::new(0);
static METADATA_AUTH_COOKIE: AtomicU64 = AtomicU64::new(0);
static METADATA_RECORD_AUTH_SEED: AtomicU64 = AtomicU64::new(0);
#[cfg(test)]
static METADATA_RECORD_AUTH_SEED_DERIVATIONS: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static METADATA_RECORD_AUTH_COMPUTATIONS: AtomicUsize = AtomicUsize::new(0);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct SemanticTypeStatsSnapshot {
    pub type_id: u64,
    pub module_id: u64,
    pub callsite: u64,
    pub allocations: usize,
    pub allocated_bytes: usize,
    pub deallocations: usize,
    pub cache_hits: usize,
    pub cache_inserts: usize,
    pub cache_bypasses: usize,
    /// Last non-zero allocation layout size observed for this exact type/module/callsite row.
    pub observed_alloc_size: usize,
    /// Last non-zero allocation layout alignment observed for this exact type/module/callsite row.
    pub observed_alloc_align: usize,
    /// Last non-zero deallocation layout size observed for this exact type/module/callsite row.
    pub observed_dealloc_size: usize,
    /// Last non-zero deallocation layout alignment observed for this exact type/module/callsite row.
    pub observed_dealloc_align: usize,
    pub policy_flags_seen: u32,
}

impl SemanticTypeStatsSnapshot {
    /// Empty caller-provided row used as scratch space for semantic type stats snapshots.
    ///
    /// Zero observed layout fields mean there is no observed layout evidence for the row.
    pub const fn empty() -> Self {
        Self {
            type_id: 0,
            module_id: 0,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }
    }
}

impl Default for SemanticTypeStatsSnapshot {
    fn default() -> Self {
        Self::empty()
    }
}

#[derive(Clone, Copy)]
struct SemanticTypeStatsSlot {
    type_id: u64,
    module_id: u64,
    callsite: u64,
    allocations: usize,
    allocated_bytes: usize,
    deallocations: usize,
    cache_hits: usize,
    cache_inserts: usize,
    cache_bypasses: usize,
    observed_alloc_size: usize,
    observed_alloc_align: usize,
    observed_dealloc_size: usize,
    observed_dealloc_align: usize,
    policy_flags_seen: u32,
}

impl SemanticTypeStatsSlot {
    const fn empty() -> Self {
        Self {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }
    }

    fn is_empty(self) -> bool {
        self.type_id == UNKNOWN_SEMANTIC_ID
    }

    fn snapshot(self) -> SemanticTypeStatsSnapshot {
        SemanticTypeStatsSnapshot {
            type_id: self.type_id,
            module_id: self.module_id,
            callsite: self.callsite,
            allocations: self.allocations,
            allocated_bytes: self.allocated_bytes,
            deallocations: self.deallocations,
            cache_hits: self.cache_hits,
            cache_inserts: self.cache_inserts,
            cache_bypasses: self.cache_bypasses,
            observed_alloc_size: self.observed_alloc_size,
            observed_alloc_align: self.observed_alloc_align,
            observed_dealloc_size: self.observed_dealloc_size,
            observed_dealloc_align: self.observed_dealloc_align,
            policy_flags_seen: self.policy_flags_seen,
        }
    }
}

struct SemanticTypeStatsShard {
    slots: [SemanticTypeStatsSlot; SEMANTIC_TYPE_STATS_SHARD_SLOTS],
}

impl SemanticTypeStatsShard {
    const fn empty() -> Self {
        Self {
            slots: [SemanticTypeStatsSlot::empty(); SEMANTIC_TYPE_STATS_SHARD_SLOTS],
        }
    }
}

const SLOW_PATH_STATS: usize = 1 << 0;
const SLOW_PATH_AUTO_METADATA: usize = 1 << 1;
const SLOW_PATH_ALLOCATION_RECORDS: usize = 1 << 2;
const SLOW_PATH_TYPE_STATS: usize = 1 << 3;
const SLOW_PATH_FLAG_BITS: usize = 4;
const SLOW_PATH_SCOPED_METADATA_UNIT: usize = 1 << SLOW_PATH_FLAG_BITS;
const SLOW_PATH_SCOPED_METADATA_MASK: usize = !(SLOW_PATH_SCOPED_METADATA_UNIT - 1);

static AUTO_COMPILER_TYPE_IDS_CURSOR: AtomicUsize = AtomicUsize::new(0);
static AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED: AtomicBool = AtomicBool::new(false);
#[thread_local]
static mut AUTO_COMPILER_TYPE_IDS_TLS_CURSOR: usize = 0;

const AUTO_COMPILER_TYPE_IDS_MODE_CYCLIC_REPLAY: usize = 1;
const AUTO_COMPILER_TYPE_IDS_MODE_CONSUMING_STREAM: usize = 2;

/// One coherent process-wide auto-metadata publication.
///
/// In particular, the compiler id pointer is never read without holding this
/// lock.  Disable/reconfigure takes the write side, so it cannot return while a
/// concurrent allocator call can still dereference the previous caller-owned
/// slice.  Keeping flags, identity context, pointer, length, mode, and recovery
/// policy together also prevents mixed-generation metadata rows.
#[derive(Clone, Copy)]
struct AutoMetadataConfig {
    flags: u32,
    module_id: u64,
    callsite: u64,
    compiler_type_ids_ptr: usize,
    compiler_type_ids_len: usize,
    compiler_type_ids_mode: usize,
    compiler_type_ids_global_recovery: bool,
}

impl AutoMetadataConfig {
    const fn disabled() -> Self {
        Self {
            flags: 0,
            module_id: 0,
            callsite: 0,
            compiler_type_ids_ptr: 0,
            compiler_type_ids_len: 0,
            compiler_type_ids_mode: 0,
            compiler_type_ids_global_recovery: true,
        }
    }

    #[inline]
    fn compiler_metadata_enabled(self) -> bool {
        self.flags != 0 && self.compiler_type_ids_ptr != 0 && self.compiler_type_ids_len != 0
    }

    #[inline]
    fn compiler_stream_enabled(self) -> bool {
        self.compiler_metadata_enabled()
            && self.compiler_type_ids_mode == AUTO_COMPILER_TYPE_IDS_MODE_CONSUMING_STREAM
    }
}

static AUTO_METADATA_CONFIG: RwLock<AutoMetadataConfig> =
    RwLock::new(AutoMetadataConfig::disabled());

#[derive(Clone, Copy)]
struct AutoLayoutMetadataHotSlot {
    size: usize,
    align: usize,
    flags: u32,
    module_id: u64,
    callsite: u64,
    metadata: AllocationMetadata,
}

impl AutoLayoutMetadataHotSlot {
    const fn empty() -> Self {
        Self {
            size: 0,
            align: 1,
            flags: 0,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            metadata: AllocationMetadata::unknown(),
        }
    }

    #[inline]
    fn matches(self, layout: Layout, flags: u32, module_id: u64, callsite: u64) -> bool {
        self.flags == flags
            && self.size == layout.size()
            && self.align == layout.align()
            && self.module_id == module_id
            && self.callsite == callsite
            && self.metadata.has_type()
            && self.metadata.is_layout_derived()
    }
}

#[derive(Clone, Copy)]
struct TypeCacheSlot {
    cache_key: u64,
    type_id: u64,
    head: *mut u8,
    count: usize,
    /// Allocator-rounded bytes represented by the linked nodes in this slot.
    ///
    /// Keeping this accounting in the slot avoids a bounded-but-hot linked-list
    /// scan on every typed free. The budget uses the allocator size class
    /// retained for each cached object, not only the requested `Layout::size`,
    /// so semantic caches cannot hide internal fragmentation behind tiny
    /// layout sizes. It is intentionally local to the TLS cache: corruption
    /// recovery still validates links before reuse and `clear()` is the single
    /// reset path for key/head/count/accounting state.
    retained_bytes: usize,
}

impl TypeCacheSlot {
    const fn empty() -> Self {
        Self {
            cache_key: UNKNOWN_SEMANTIC_ID,
            type_id: UNKNOWN_SEMANTIC_ID,
            head: core::ptr::null_mut(),
            count: 0,
            retained_bytes: 0,
        }
    }

    #[inline]
    fn has_corrupt_links(self) -> bool {
        self.count > MAX_TYPE_CACHE_DEPTH
            || (self.count == 0 && !self.head.is_null())
            || (self.count != 0 && self.head.is_null())
    }

    #[inline]
    fn has_corrupt_accounting(self) -> bool {
        self.retained_bytes > MAX_TYPE_CACHE_SLOT_BYTES
            || (self.count == 0 && self.retained_bytes != 0)
            || (self.count != 0 && self.retained_bytes == 0)
    }

    #[inline]
    fn is_corrupt(self) -> bool {
        self.has_corrupt_links() || self.has_corrupt_accounting()
    }

    #[inline]
    fn clear(&mut self) {
        *self = Self::empty();
    }
}

#[derive(Clone, Copy)]
struct TypeCacheHotSlot {
    cache_key: u64,
    slot_idx: usize,
}

impl TypeCacheHotSlot {
    const fn empty() -> Self {
        Self {
            cache_key: UNKNOWN_SEMANTIC_ID,
            slot_idx: 0,
        }
    }
}

#[derive(Clone, Copy)]
struct TypeCacheIdentityHotSlot {
    metadata: AllocationMetadata,
    cache_key: u64,
    active: bool,
}

impl TypeCacheIdentityHotSlot {
    const fn empty() -> Self {
        Self {
            metadata: AllocationMetadata::unknown(),
            cache_key: UNKNOWN_SEMANTIC_ID,
            active: false,
        }
    }
}

#[derive(Clone, Copy)]
struct MetadataRecordAuthHotSlot {
    seed: u64,
    ptr: usize,
    size: usize,
    align: usize,
    metadata: AllocationMetadata,
    auth: u64,
    active: bool,
}

impl MetadataRecordAuthHotSlot {
    const fn empty() -> Self {
        Self {
            seed: 0,
            ptr: 0,
            size: 0,
            align: 1,
            metadata: AllocationMetadata::unknown(),
            auth: 0,
            active: false,
        }
    }

    #[inline]
    fn matches(
        self,
        seed: u64,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> bool {
        self.active
            && self.seed == seed
            && self.ptr == ptr as usize
            && self.size == layout.size()
            && self.align == layout.align()
            && self.metadata == metadata
    }
}

#[derive(Clone, Copy)]
struct InlineTypeCacheEntry {
    cache_key: u64,
    type_id: u64,
    ptr: *mut u8,
    size: usize,
    align: usize,
}

impl InlineTypeCacheEntry {
    const fn empty() -> Self {
        Self {
            cache_key: UNKNOWN_SEMANTIC_ID,
            type_id: UNKNOWN_SEMANTIC_ID,
            ptr: core::ptr::null_mut(),
            size: 0,
            align: 1,
        }
    }

    #[inline]
    fn is_empty(self) -> bool {
        self.ptr.is_null()
    }

    #[inline]
    fn matches(self, layout: Layout, metadata: AllocationMetadata) -> bool {
        self.matches_cached_key(layout, type_cache_identity_key(metadata))
    }

    #[inline]
    fn matches_cached_key(self, layout: Layout, cache_key: u64) -> bool {
        self.cache_key == cache_key
            && self.size == layout.size()
            && self.align == layout.align()
            && !self.ptr.is_null()
    }

    #[inline]
    fn retained_bytes(self) -> usize {
        type_cache_retained_bytes_for_object(self.size)
    }
}

#[thread_local]
static mut TYPE_CACHE: [TypeCacheSlot; TYPE_CACHE_SLOTS] =
    [TypeCacheSlot::empty(); TYPE_CACHE_SLOTS];

#[thread_local]
static mut TYPE_CACHE_HOT_SLOT: TypeCacheHotSlot = TypeCacheHotSlot::empty();

#[thread_local]
static mut TYPE_CACHE_IDENTITY_HOT_SLOT: TypeCacheIdentityHotSlot =
    TypeCacheIdentityHotSlot::empty();

#[cfg(not(unialloc_target_arm64e))]
#[thread_local]
static mut METADATA_RECORD_AUTH_HOT_SLOT: MetadataRecordAuthHotSlot =
    MetadataRecordAuthHotSlot::empty();

#[cfg(test)]
static TYPE_CACHE_IDENTITY_HOT_HITS: AtomicUsize = AtomicUsize::new(0);

#[thread_local]
static mut INLINE_TYPE_CACHE_ENTRY: InlineTypeCacheEntry = InlineTypeCacheEntry::empty();

#[derive(Clone, Copy)]
struct SegregatedTypeCacheEntry {
    cache_key: u64,
    type_id: u64,
    policy_key: u32,
    ptr: *mut u8,
    size: usize,
    align: usize,
    auth: u64,
    metadata: AllocationMetadata,
}

impl SegregatedTypeCacheEntry {
    const fn empty() -> Self {
        Self {
            cache_key: UNKNOWN_SEMANTIC_ID,
            type_id: UNKNOWN_SEMANTIC_ID,
            policy_key: 0,
            ptr: core::ptr::null_mut(),
            size: 0,
            align: 1,
            auth: 0,
            metadata: AllocationMetadata::unknown(),
        }
    }

    fn is_empty(self) -> bool {
        self.ptr.is_null()
    }

    #[inline]
    fn matches_cached_key(self, layout: Layout, cache_key: u64, policy_key: u32) -> bool {
        self.cache_key == cache_key
            && self.size == layout.size()
            && self.align == layout.align()
            && !self.ptr.is_null()
            && self.policy_key == policy_key
    }

    #[inline]
    fn retained_bytes(self) -> usize {
        type_cache_retained_bytes_for_object(self.size)
    }
}

// The entry owns only an allocator object pointer plus metadata authenticator.
// On arm64e the diagnostic no_std side-cache stores this single-entry hot path
// behind a spin mutex so it can avoid Rust TLV descriptors while still
// exercising the real PAC metadata record used by the normal segregated cache.
unsafe impl Send for SegregatedTypeCacheEntry {}

#[derive(Clone, Copy)]
struct SegregatedTypeCacheBucket {
    entries: [SegregatedTypeCacheEntry; SEGREGATED_TYPE_CACHE_DEPTH],
    count: usize,
    evict_cursor: usize,
    /// Allocator-rounded bytes stored in the compact active prefix of `entries`.
    ///
    /// The metadata-segregated cache has richer entries than the plain linked
    /// type-cache, but it is still on the typed free hot path.  Keep the byte
    /// budget as explicit bucket state so `can_accept_object_size` does not
    /// rescan the active prefix after the normal corruption check has already
    /// selected a bucket.
    retained_bytes: usize,
}

impl SegregatedTypeCacheBucket {
    const fn empty() -> Self {
        Self {
            entries: [SegregatedTypeCacheEntry::empty(); SEGREGATED_TYPE_CACHE_DEPTH],
            count: 0,
            evict_cursor: 0,
            retained_bytes: 0,
        }
    }

    fn retained_bytes(&self) -> Option<usize> {
        if self.is_structurally_corrupt() {
            return None;
        }

        let mut total = 0usize;
        let mut idx = 0usize;
        while idx < self.count {
            let entry = self.entries[idx];
            if Layout::from_size_align(entry.size, entry.align).is_err() {
                return None;
            }
            total = total.saturating_add(entry.retained_bytes());
            idx += 1;
        }
        Some(total)
    }

    fn can_accept_object_size(&self, incoming_size: usize) -> bool {
        if self.is_corrupt() {
            return false;
        }
        debug_assert_eq!(
            self.retained_bytes(),
            Some(self.retained_bytes),
            "segregated type-cache retained-byte counter must match entries"
        );
        let incoming_retained_bytes = type_cache_retained_bytes_for_object(incoming_size);
        let projected = if self.count < SEGREGATED_TYPE_CACHE_DEPTH {
            self.retained_bytes.saturating_add(incoming_retained_bytes)
        } else {
            let evict_idx = self.evict_cursor & (SEGREGATED_TYPE_CACHE_DEPTH - 1);
            self.retained_bytes
                .saturating_sub(self.entries[evict_idx].retained_bytes())
                .saturating_add(incoming_retained_bytes)
        };
        projected <= MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES
    }

    fn pop(
        &mut self,
        layout: Layout,
        cache_key: u64,
        policy_key: u32,
    ) -> Option<SegregatedTypeCacheEntry> {
        if self.clear_if_corrupt() {
            return None;
        }

        let mut idx = self.count;
        while idx > 0 {
            idx -= 1;
            let entry = self.entries[idx];
            if entry.matches_cached_key(layout, cache_key, policy_key) {
                let last_idx = self.count - 1;
                self.entries[idx] = self.entries[last_idx];
                self.entries[last_idx] = SegregatedTypeCacheEntry::empty();
                self.count -= 1;
                self.retained_bytes = self.retained_bytes.saturating_sub(entry.retained_bytes());
                if self.count < SEGREGATED_TYPE_CACHE_DEPTH {
                    self.evict_cursor = 0;
                }
                if self.count == 0 {
                    self.retained_bytes = 0;
                } else if !self.repair_accounting_if_possible() {
                    *self = Self::empty();
                }
                return Some(entry);
            }
        }
        None
    }

    fn push(&mut self, entry: SegregatedTypeCacheEntry) -> Option<SegregatedTypeCacheEntry> {
        self.clear_if_corrupt();

        if self.count < SEGREGATED_TYPE_CACHE_DEPTH {
            self.entries[self.count] = entry;
            self.count += 1;
            self.retained_bytes = self.retained_bytes.saturating_add(entry.retained_bytes());
            return None;
        }

        let idx = self.evict_cursor & (SEGREGATED_TYPE_CACHE_DEPTH - 1);
        let evicted = self.entries[idx];
        self.entries[idx] = entry;
        self.retained_bytes = self
            .retained_bytes
            .saturating_sub(evicted.retained_bytes())
            .saturating_add(entry.retained_bytes());
        self.evict_cursor = (idx + 1) & (SEGREGATED_TYPE_CACHE_DEPTH - 1);
        Some(evicted)
    }

    fn matching_entry_count(&self, layout: Layout, cache_key: u64, policy_key: u32) -> usize {
        if self.is_corrupt() {
            return 0;
        }

        let mut count = 0;
        let mut idx = 0;
        while idx < self.count {
            if self.entries[idx].matches_cached_key(layout, cache_key, policy_key) {
                count += 1;
            }
            idx += 1;
        }
        count
    }

    #[inline]
    fn has_corrupt_accounting(&self) -> bool {
        self.retained_bytes > MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES
            || (self.count == 0 && self.retained_bytes != 0)
            || (self.count != 0 && self.retained_bytes == 0)
    }

    #[inline]
    fn is_corrupt(&self) -> bool {
        self.is_structurally_corrupt() || self.has_corrupt_accounting()
    }

    #[inline]
    fn is_structurally_corrupt(&self) -> bool {
        if self.count > SEGREGATED_TYPE_CACHE_DEPTH {
            return true;
        }

        let mut idx = 0usize;
        while idx < SEGREGATED_TYPE_CACHE_DEPTH {
            let occupied = !self.entries[idx].is_empty();
            if idx < self.count {
                if !occupied {
                    return true;
                }
            } else if occupied {
                return true;
            }
            idx += 1;
        }
        false
    }

    #[inline]
    fn repair_accounting_if_possible(&mut self) -> bool {
        if self.is_structurally_corrupt() {
            return false;
        }
        if !self.has_corrupt_accounting() {
            return true;
        }

        // The retained-byte counter is a hot-path budget hint, not ownership
        // metadata.  When the compact entry prefix is still valid, recompute the
        // counter instead of dropping owned cached objects on counter-only drift.
        // Structural corruption still clears the bucket through `clear_if_corrupt`.
        match self.retained_bytes() {
            Some(total)
                if total <= MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES
                    && ((self.count == 0 && total == 0) || (self.count != 0 && total != 0)) =>
            {
                self.retained_bytes = total;
                true
            }
            _ => false,
        }
    }

    #[inline]
    fn clear_if_corrupt(&mut self) -> bool {
        if self.repair_accounting_if_possible() {
            return false;
        }
        *self = Self::empty();
        true
    }
}

#[derive(Clone, Copy)]
struct SegregatedTypeCacheHotBucket {
    cache_key: u64,
    size: usize,
    align: usize,
    policy_key: u32,
    cache_domain: u8,
    bucket_idx: usize,
}

impl SegregatedTypeCacheHotBucket {
    const fn empty() -> Self {
        Self {
            cache_key: UNKNOWN_SEMANTIC_ID,
            size: 0,
            align: 1,
            policy_key: 0,
            cache_domain: SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
            bucket_idx: 0,
        }
    }

    #[inline]
    fn matches_cached_key(
        self,
        layout: Layout,
        cache_key: u64,
        policy_key: u32,
        cache_domain: u8,
    ) -> bool {
        self.cache_key == cache_key
            && self.size == layout.size()
            && self.align == layout.align()
            && self.policy_key == policy_key
            && self.cache_domain == cache_domain
    }

    #[inline]
    fn matches(self, layout: Layout, metadata: AllocationMetadata) -> bool {
        self.matches_cached_key(
            layout,
            type_cache_identity_key(metadata),
            segregated_type_cache_policy_key(metadata),
            SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
        )
    }
}

#[thread_local]
static mut SEGREGATED_TYPE_CACHE: [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS] =
    [SegregatedTypeCacheBucket::empty(); TYPE_CACHE_SLOTS];

#[thread_local]
static mut SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS: usize = 0;

#[thread_local]
static mut SEGREGATED_TYPE_CACHE_RETAINED_BYTES: usize = 0;

#[thread_local]
static mut SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED: bool = false;

const ORDINARY_INLINE_SEGREGATED_TYPE_CACHE_DEPTH: usize = 2;

#[thread_local]
static mut INLINE_SEGREGATED_TYPE_CACHE_ENTRY: SegregatedTypeCacheEntry =
    SegregatedTypeCacheEntry::empty();

// Two inline entries keep a small alternating semantic pair off the bucket
// table. Larger working sets still spill into the existing bounded cache.
#[thread_local]
static mut INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND: SegregatedTypeCacheEntry =
    SegregatedTypeCacheEntry::empty();

#[cfg(not(feature = "fixed_heap"))]
const HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_DEPTH: usize = 2;

#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY: SegregatedTypeCacheEntry =
    SegregatedTypeCacheEntry::empty();

// Two inline entries cover the common producer/consumer pair without
// materializing a page-backed bucket table.  Larger working sets still spill
// into the bounded side cache, preserving the existing aggregate byte caps.
#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND: SegregatedTypeCacheEntry =
    SegregatedTypeCacheEntry::empty();

#[cfg(unialloc_target_arm64e)]
static ARM64E_INLINE_SEGREGATED_TYPE_CACHE_ENTRY: Mutex<SegregatedTypeCacheEntry> =
    Mutex::new(SegregatedTypeCacheEntry::empty());

#[thread_local]
static mut SEGREGATED_TYPE_CACHE_HOT_BUCKET: SegregatedTypeCacheHotBucket =
    SegregatedTypeCacheHotBucket::empty();

#[cfg(test)]
static SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS: AtomicUsize = AtomicUsize::new(0);

#[cfg(test)]
static SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS: AtomicUsize = AtomicUsize::new(0);

#[cfg(test)]
#[inline]
fn record_segregated_type_cache_bucket_probe_step() {
    SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.fetch_add(1, Ordering::Relaxed);
}

#[cfg(not(test))]
#[inline]
fn record_segregated_type_cache_bucket_probe_step() {}

#[cfg(test)]
#[inline]
fn record_segregated_type_cache_aggregate_scan() {
    SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.fetch_add(1, Ordering::Relaxed);
}

#[cfg(not(test))]
#[inline]
fn record_segregated_type_cache_aggregate_scan() {}

#[inline]
fn segregated_type_cache_probe_already_checked(checked_hot_idx: Option<usize>, idx: usize) -> bool {
    checked_hot_idx == Some(idx)
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
unsafe fn note_segregated_type_cache_bucket_occupancy_change(
    cache_domain: u8,
    was_occupied: bool,
    is_occupied: bool,
) {
    if was_occupied == is_occupied {
        return;
    }
    match cache_domain {
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY => {
            if is_occupied {
                SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS =
                    SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS.saturating_add(1);
            } else {
                SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS =
                    SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS.saturating_sub(1);
            }
        }
        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE => {
            if is_occupied {
                HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS =
                    HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS.saturating_add(1);
            } else {
                HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS =
                    HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS.saturating_sub(1);
            }
        }
        _ => {}
    }
}

#[inline]
unsafe fn segregated_type_cache_occupied_bucket_count(cache_domain: u8) -> usize {
    match cache_domain {
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY => SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS,
        #[cfg(not(feature = "fixed_heap"))]
        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE => HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS,
        _ => 0,
    }
}

#[inline]
fn segregated_type_cache_inline_domain(_metadata: AllocationMetadata) -> u8 {
    #[cfg(not(feature = "fixed_heap"))]
    {
        if _metadata.requests(FLAG_HUGEPAGE_METADATA) {
            return SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE;
        }
    }
    SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY
}

#[inline]
unsafe fn inline_segregated_type_cache_entry(_cache_domain: u8) -> *mut SegregatedTypeCacheEntry {
    inline_segregated_type_cache_entry_at(_cache_domain, 0)
}

#[inline]
fn inline_segregated_type_cache_capacity(_cache_domain: u8) -> usize {
    #[cfg(not(feature = "fixed_heap"))]
    {
        if _cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE {
            return HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_DEPTH;
        }
    }
    ORDINARY_INLINE_SEGREGATED_TYPE_CACHE_DEPTH
}

#[inline]
unsafe fn inline_segregated_type_cache_entry_at(
    _cache_domain: u8,
    index: usize,
) -> *mut SegregatedTypeCacheEntry {
    debug_assert!(index < inline_segregated_type_cache_capacity(_cache_domain));
    #[cfg(not(feature = "fixed_heap"))]
    {
        if _cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE {
            return if index == 0 {
                &raw mut HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY
            } else {
                &raw mut HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND
            };
        }
    }
    if index == 0 {
        &raw mut INLINE_SEGREGATED_TYPE_CACHE_ENTRY
    } else {
        &raw mut INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND
    }
}

#[inline]
unsafe fn segregated_type_cache_bucket_retained_bytes(cache_domain: u8) -> usize {
    match cache_domain {
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY => SEGREGATED_TYPE_CACHE_RETAINED_BYTES,
        #[cfg(not(feature = "fixed_heap"))]
        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE => HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES,
        _ => 0,
    }
}

#[inline]
unsafe fn segregated_type_cache_bucket_retained_bytes_trusted(cache_domain: u8) -> bool {
    match cache_domain {
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY => SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED,
        #[cfg(not(feature = "fixed_heap"))]
        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE => {
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED
        }
        _ => false,
    }
}

#[inline]
unsafe fn set_segregated_type_cache_bucket_retained_bytes(
    cache_domain: u8,
    retained_bytes: usize,
    trusted: bool,
) {
    match cache_domain {
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY => {
            SEGREGATED_TYPE_CACHE_RETAINED_BYTES = retained_bytes;
            SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = trusted;
        }
        #[cfg(not(feature = "fixed_heap"))]
        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE => {
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES = retained_bytes;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = trusted;
        }
        _ => {}
    }
}

#[inline]
unsafe fn mark_segregated_type_cache_retained_bytes_untrusted(cache_domain: u8) {
    match cache_domain {
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY => {
            SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
        }
        #[cfg(not(feature = "fixed_heap"))]
        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE => {
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
        }
        _ => {}
    }
}

#[inline]
unsafe fn adjust_trusted_segregated_type_cache_retained_bytes(
    cache_domain: u8,
    released_bytes: usize,
    retained_bytes: usize,
) {
    if !segregated_type_cache_bucket_retained_bytes_trusted(cache_domain) {
        return;
    }
    let updated = segregated_type_cache_bucket_retained_bytes(cache_domain)
        .saturating_sub(released_bytes)
        .saturating_add(retained_bytes);
    set_segregated_type_cache_bucket_retained_bytes(cache_domain, updated, true);
}

#[cfg(feature = "fixed_heap")]
#[inline]
unsafe fn note_segregated_type_cache_bucket_occupancy_change(
    cache_domain: u8,
    was_occupied: bool,
    is_occupied: bool,
) {
    if cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY && was_occupied != is_occupied {
        if is_occupied {
            SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS =
                SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS.saturating_add(1);
        } else {
            SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS =
                SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS.saturating_sub(1);
        }
    }
}

#[inline]
unsafe fn clear_segregated_type_cache_bucket_if_corrupt(
    bucket: &mut SegregatedTypeCacheBucket,
    cache_domain: u8,
) -> bool {
    let was_occupied = bucket.count != 0;
    let cleared = bucket.clear_if_corrupt();
    if cleared {
        note_segregated_type_cache_bucket_occupancy_change(
            cache_domain,
            was_occupied,
            bucket.count != 0,
        );
        mark_segregated_type_cache_retained_bytes_untrusted(cache_domain);
    }
    cleared
}

#[inline]
unsafe fn pop_segregated_type_cache_bucket(
    bucket: &mut SegregatedTypeCacheBucket,
    cache_domain: u8,
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
) -> Option<SegregatedTypeCacheEntry> {
    let was_occupied = bucket.count != 0;
    let was_corrupt = bucket.is_corrupt();
    let popped = bucket.pop(layout, cache_key, policy_key);
    note_segregated_type_cache_bucket_occupancy_change(
        cache_domain,
        was_occupied,
        bucket.count != 0,
    );
    if was_corrupt {
        mark_segregated_type_cache_retained_bytes_untrusted(cache_domain);
    } else if let Some(entry) = popped {
        adjust_trusted_segregated_type_cache_retained_bytes(
            cache_domain,
            entry.retained_bytes(),
            0,
        );
    }
    popped
}

#[inline]
unsafe fn push_segregated_type_cache_bucket(
    bucket: &mut SegregatedTypeCacheBucket,
    cache_domain: u8,
    entry: SegregatedTypeCacheEntry,
) -> Option<SegregatedTypeCacheEntry> {
    let was_occupied = bucket.count != 0;
    let was_corrupt = bucket.is_corrupt();
    let evicted = bucket.push(entry);
    note_segregated_type_cache_bucket_occupancy_change(
        cache_domain,
        was_occupied,
        bucket.count != 0,
    );
    if was_corrupt {
        mark_segregated_type_cache_retained_bytes_untrusted(cache_domain);
    } else {
        adjust_trusted_segregated_type_cache_retained_bytes(
            cache_domain,
            evicted.map(|entry| entry.retained_bytes()).unwrap_or(0),
            entry.retained_bytes(),
        );
    }
    evicted
}

#[thread_local]
static mut AUTO_LAYOUT_METADATA_HOT_SLOT: AutoLayoutMetadataHotSlot =
    AutoLayoutMetadataHotSlot::empty();

#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_SEGREGATED_TYPE_CACHE: *mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS] =
    core::ptr::null_mut();
#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE: usize = 0;
#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING: system_alloc::HugePageMmapBacking =
    system_alloc::HugePageMmapBacking::Unmapped;
#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS: usize = 0;
#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES: usize = 0;
#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED: bool = false;

#[inline]
unsafe fn ordinary_segregated_type_cache_mut(
) -> &'static mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS] {
    // The ordinary segregated type cache is thread-local allocator state.
    // Callers keep this borrow local to one pop/push operation; `addr_of_mut!`
    // avoids creating an implicit reference to `static mut` before that local
    // aliasing boundary is established.
    let ptr = core::ptr::addr_of_mut!(SEGREGATED_TYPE_CACHE);
    match ptr.as_mut() {
        Some(cache) => cache,
        None => core::hint::unreachable_unchecked(),
    }
}

#[cfg(not(feature = "fixed_heap"))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub struct HugepageMetadataSideCacheSnapshot {
    pub allocated: bool,
    pub address: usize,
    pub mapping_size: usize,
    pub backing: system_alloc::HugePageMmapBacking,
    /// Number of non-inline hugepage side-cache buckets with retained or corrupt-visible state.
    pub occupied_buckets: usize,
    /// Number of cached entries visible in the hugepage side-cache bucket table.
    pub occupied_entries: usize,
    /// Trusted allocator-rounded bytes retained by healthy hugepage side-cache buckets.
    ///
    /// Corrupt buckets are reported through `corrupt_buckets` and excluded from
    /// this total, so footprint gates do not trust already-invalid accounting.
    pub retained_bytes: usize,
    pub corrupt_buckets: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SegregatedTypeCacheFootprintSnapshot {
    occupied_buckets: usize,
    occupied_entries: usize,
    retained_bytes: usize,
    corrupt_buckets: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub struct MetadataSegregationSideCacheSnapshot {
    pub inline_occupied: bool,
    /// Number of non-inline table buckets with retained or corrupt-visible state.
    ///
    /// The one-entry inline hot slot is reported through `inline_occupied` and
    /// contributes one `occupied_entries` value, but it is not a table bucket.
    /// Keeping that distinction matches `TypeIsolationSideCacheSnapshot`, where
    /// the inline slot contributes an entry but not an occupied cold slot.  A
    /// corrupt bucket still contributes here when its count field or entry array
    /// shows occupied state; its byte counter is intentionally excluded from
    /// `retained_bytes`.
    pub occupied_buckets: usize,
    pub occupied_entries: usize,
    /// Trusted allocator-rounded bytes retained by the inline entry plus non-corrupt buckets.
    ///
    /// Corrupt buckets are counted separately and intentionally excluded so a
    /// footprint check does not trust already-invalid side-cache accounting.
    pub retained_bytes: usize,
    pub corrupt_buckets: usize,
    pub hot_bucket_active: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub struct TypeIsolationSideCacheSnapshot {
    pub inline_occupied: bool,
    pub occupied_slots: usize,
    pub occupied_entries: usize,
    /// Trusted allocator-rounded bytes retained by the inline entry plus non-corrupt slots.
    ///
    /// Corrupt occupied slots remain visible through `occupied_slots` and
    /// `corrupt_slots`, but their byte counters are not trusted in this total.
    pub retained_bytes: usize,
    pub corrupt_slots: usize,
    pub hot_slot_active: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub struct DelayedFreeSnapshot {
    pub occupied_slots: usize,
    /// Allocator-rounded retained bytes found by scanning the delayed-free slots.
    pub retained_bytes: usize,
    /// Allocator-rounded retained bytes tracked by the hot-path quarantine counter.
    ///
    /// Keeping this beside the scanned value lets tests and diagnostics detect
    /// byte-accounting drift without changing the enqueue/evict fast path.
    pub tracked_retained_bytes: usize,
    /// Whether the maintained retained-byte counter matches the slot scan.
    pub retained_accounting_matches: bool,
    pub cursor: usize,
}

#[derive(Clone, Copy)]
struct DelayedFreeSlot {
    ptr: *mut u8,
    size: usize,
    align: usize,
    auth: u64,
    metadata: AllocationMetadata,
}

impl DelayedFreeSlot {
    const fn empty() -> Self {
        Self {
            ptr: core::ptr::null_mut(),
            size: 0,
            align: 1,
            auth: 0,
            metadata: AllocationMetadata::unknown(),
        }
    }

    fn is_empty(self) -> bool {
        self.ptr.is_null()
    }

    #[inline]
    fn retained_bytes(self) -> usize {
        delayed_free_retained_bytes_for_object(self.size)
    }
}

#[derive(Clone, Copy)]
struct TaggedAllocation {
    ptr: *mut u8,
    size: usize,
    align: usize,
    tag: u64,
    metadata: AllocationMetadata,
}

impl TaggedAllocation {
    const fn empty() -> Self {
        Self {
            ptr: core::ptr::null_mut(),
            size: 0,
            align: 1,
            tag: UNKNOWN_SEMANTIC_ID,
            metadata: AllocationMetadata::unknown(),
        }
    }

    fn is_empty(self) -> bool {
        self.ptr.is_null()
    }
}

// Cross-thread memory-tag recovery stores raw allocation addresses behind a
// process-global mutex.  The pointer is never dereferenced through the table;
// it is only compared and passed back to the allocator after metadata/tag
// validation, so guarded transfer between threads is sound at this layer.
unsafe impl Send for TaggedAllocation {}

#[derive(Clone, Copy)]
struct MemoryTagHotSlot {
    ptr: usize,
    slot_idx: usize,
}

impl MemoryTagHotSlot {
    const fn empty() -> Self {
        Self {
            ptr: 0,
            slot_idx: 0,
        }
    }
}

#[cfg(not(feature = "fixed_heap"))]
struct MemoryTagPage {
    next: *mut MemoryTagPage,
    entries: [TaggedAllocation; MEMORY_TAG_PAGE_SLOTS],
}

#[cfg(not(feature = "fixed_heap"))]
impl MemoryTagPage {
    const fn empty() -> Self {
        Self {
            next: core::ptr::null_mut(),
            entries: [TaggedAllocation::empty(); MEMORY_TAG_PAGE_SLOTS],
        }
    }
}

struct GlobalMemoryTagTable {
    fast: [TaggedAllocation; GLOBAL_MEMORY_TAG_SHARD_SLOTS],
    hot: MemoryTagHotSlot,
    #[cfg(not(feature = "fixed_heap"))]
    overflow: *mut MemoryTagPage,
}

impl GlobalMemoryTagTable {
    const fn empty() -> Self {
        Self {
            fast: [TaggedAllocation::empty(); GLOBAL_MEMORY_TAG_SHARD_SLOTS],
            hot: MemoryTagHotSlot::empty(),
            #[cfg(not(feature = "fixed_heap"))]
            overflow: core::ptr::null_mut(),
        }
    }
}

// Cross-thread memory-tag overflow state is protected by its shard spin mutex.
// The raw overflow-page pointer is never accessed without that shard lock and
// points to pages owned by the table, so transferring the table between threads
// under the mutex is sound at this layer.
unsafe impl Send for GlobalMemoryTagTable {}

#[thread_local]
static mut DELAYED_FREE: [DelayedFreeSlot; DELAYED_FREE_SLOTS] =
    [DelayedFreeSlot::empty(); DELAYED_FREE_SLOTS];

#[thread_local]
static mut DELAYED_FREE_CURSOR: usize = 0;

#[thread_local]
static mut DELAYED_FREE_RETAINED_BYTES: usize = 0;

// Exact-but-defensive occupancy hint for the small delayed-free quarantine.
// Retained bytes and the slots remain the authority: eviction checks rebuild
// this mask if the bytes represented by set bits do not match the maintained
// retained-byte counter.  That keeps the normal overflow path from scanning
// empty slots while preventing a stale mask from hiding retained objects.
#[thread_local]
static mut DELAYED_FREE_OCCUPIED_MASK: usize = 0;

#[thread_local]
static mut MEMORY_TAGS: [TaggedAllocation; MEMORY_TAG_FAST_SLOTS] =
    [TaggedAllocation::empty(); MEMORY_TAG_FAST_SLOTS];

#[thread_local]
static mut MEMORY_TAG_HOT_SLOT: MemoryTagHotSlot = MemoryTagHotSlot::empty();

#[thread_local]
static mut MEMORY_TAG_RECORD_COUNT: usize = 0;

static GLOBAL_MEMORY_TAGS: [Mutex<GlobalMemoryTagTable>; GLOBAL_MEMORY_TAG_SHARD_COUNT] = [
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
    Mutex::new(GlobalMemoryTagTable::empty()),
];
static GLOBAL_MEMORY_TAG_RECORD_COUNT: AtomicUsize = AtomicUsize::new(0);

#[cfg(not(feature = "fixed_heap"))]
#[thread_local]
static mut MEMORY_TAG_OVERFLOW: *mut MemoryTagPage = core::ptr::null_mut();

#[thread_local]
static mut ACTIVE_METADATA: AllocationMetadata = AllocationMetadata::unknown();

#[thread_local]
static mut SEMANTIC_SCOPE_STACK: [AllocationMetadata; SEMANTIC_SCOPE_STACK_CAPACITY] =
    [AllocationMetadata::unknown(); SEMANTIC_SCOPE_STACK_CAPACITY];

#[thread_local]
static mut SEMANTIC_SCOPE_STACK_DEPTH: usize = 0;

#[thread_local]
static mut SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH: usize = 0;

#[thread_local]
static mut SEMANTIC_SCOPE_OVERFLOW_STACK: [AllocationMetadata;
    SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY] =
    [AllocationMetadata::unknown(); SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY];

#[thread_local]
static mut SEMANTIC_SCOPE_OVERFLOW_BOUNDARY_ACTIVE: AllocationMetadata =
    AllocationMetadata::unknown();

#[thread_local]
static mut SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS: bool = false;

#[thread_local]
static mut AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH: usize = 0;

static SEMANTIC_TYPE_STATS: [Mutex<SemanticTypeStatsShard>; SEMANTIC_TYPE_STATS_SHARD_COUNT] = [
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
    Mutex::new(SemanticTypeStatsShard::empty()),
];

/// Bitset of semantic-stat shards that contain at least one live row.
///
/// The sharded table deliberately preserves the old fixed row budget, so sparse
/// workloads should not pay to lock every shard when only one or two type rows
/// exist.  The bit is only a fast-path hint: it is set while holding the shard
/// that receives the first row, and cleared only by reset (semantic type rows
/// are append-only between resets).
static SEMANTIC_TYPE_STATS_ACTIVE_SHARDS: AtomicUsize = AtomicUsize::new(0);

#[inline]
fn atomic_saturating_decrement(counter: &AtomicUsize) -> usize {
    let mut current = counter.load(Ordering::Relaxed);
    loop {
        if current == 0 {
            return 0;
        }
        let next = current - 1;
        match counter.compare_exchange_weak(current, next, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return next,
            Err(actual) => current = actual,
        }
    }
}

#[derive(Clone, Copy)]
struct AutoAllocationRecord {
    ptr: usize,
    size: usize,
    align: usize,
    auth: u64,
    metadata: AllocationMetadata,
}

impl AutoAllocationRecord {
    const fn empty() -> Self {
        Self {
            ptr: 0,
            size: 0,
            align: 1,
            auth: 0,
            metadata: AllocationMetadata::unknown(),
        }
    }

    const fn tombstone() -> Self {
        Self {
            ptr: AUTO_ALLOCATION_RECORD_TOMBSTONE_PTR,
            size: 0,
            align: 1,
            auth: 0,
            metadata: AllocationMetadata::unknown(),
        }
    }

    fn is_empty(self) -> bool {
        self.ptr == 0
    }

    fn is_tombstone(self) -> bool {
        self.ptr == AUTO_ALLOCATION_RECORD_TOMBSTONE_PTR
    }

    fn is_available(self) -> bool {
        self.is_empty() || self.is_tombstone()
    }

    #[inline]
    fn matches_allocation(self, ptr: *mut u8, layout: Layout) -> bool {
        self.ptr == ptr as usize
            && self.size == layout.size()
            && self.align == layout.align()
            && self.auth == derive_auto_allocation_record_auth(ptr, layout, self.metadata)
    }
}

#[cfg(not(feature = "fixed_heap"))]
struct AutoAllocationRecordPage {
    next: *mut AutoAllocationRecordPage,
    entries: [AutoAllocationRecord; AUTO_ALLOCATION_RECORD_OVERFLOW_SLOTS],
}

#[cfg(not(feature = "fixed_heap"))]
impl AutoAllocationRecordPage {
    const fn empty() -> Self {
        Self {
            next: core::ptr::null_mut(),
            entries: [AutoAllocationRecord::empty(); AUTO_ALLOCATION_RECORD_OVERFLOW_SLOTS],
        }
    }
}

#[derive(Clone, Copy)]
struct FastAutoAllocationRecordHotSlot {
    ptr: usize,
    slot_idx: usize,
}

impl FastAutoAllocationRecordHotSlot {
    const fn empty() -> Self {
        Self {
            ptr: 0,
            slot_idx: 0,
        }
    }
}

type GlobalAutoAllocationRecordHotSlot = FastAutoAllocationRecordHotSlot;

struct GlobalAutoAllocationRecordTable {
    inline: [AutoAllocationRecord; AUTO_ALLOCATION_RECORD_SHARD_SLOTS],
    hot: GlobalAutoAllocationRecordHotSlot,
    live_count: usize,
    #[cfg(not(feature = "fixed_heap"))]
    overflow: *mut AutoAllocationRecordPage,
}

impl GlobalAutoAllocationRecordTable {
    const fn empty() -> Self {
        Self {
            inline: [AutoAllocationRecord::empty(); AUTO_ALLOCATION_RECORD_SHARD_SLOTS],
            hot: GlobalAutoAllocationRecordHotSlot::empty(),
            live_count: 0,
            #[cfg(not(feature = "fixed_heap"))]
            overflow: core::ptr::null_mut(),
        }
    }
}

// Each global compiler-allocation recovery shard owns overflow pages behind its
// shard-local spin mutex.  The raw page pointer is only followed while that
// shard mutex is held, so guarded transfer between threads is sound at this
// bookkeeping layer.
unsafe impl Send for GlobalAutoAllocationRecordTable {}

static AUTO_ALLOCATION_RECORDS: [Mutex<GlobalAutoAllocationRecordTable>;
    AUTO_ALLOCATION_RECORD_SHARD_COUNT] = [
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
    Mutex::new(GlobalAutoAllocationRecordTable::empty()),
];
static AUTO_ALLOCATION_RECORD_COUNT: AtomicUsize = AtomicUsize::new(0);
/// Bitset of global recovery shards with at least one live record.
///
/// The count is still the source of truth for "does any recovery record exist?"
/// This bitset is an exact-but-defensive lock-skipping hint for sparse sharded
/// traffic: each insertion sets its shard bit before publishing the record, and
/// removals clear the bit only after scanning that shard under the shard lock.
/// If a test or legacy path seeds records without setting the bit, lookups fall
/// back to scanning all shards when the mask is zero but the global count is not.
/// When the mask is nonzero, lookup still forces the pointer's home shard into
/// the probe set so a stale hint cannot hide the authoritative home record.
static AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS: AtomicUsize = AtomicUsize::new(0);

#[thread_local]
static mut FAST_AUTO_ALLOCATION_RECORDS: [AutoAllocationRecord; FAST_AUTO_ALLOCATION_RECORD_SLOTS] =
    [AutoAllocationRecord::empty(); FAST_AUTO_ALLOCATION_RECORD_SLOTS];

#[thread_local]
static mut FAST_AUTO_ALLOCATION_RECORD_INLINE: AutoAllocationRecord = AutoAllocationRecord::empty();

#[thread_local]
static mut FAST_AUTO_ALLOCATION_RECORD_COUNT: usize = 0;

#[thread_local]
static mut FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT: FastAutoAllocationRecordHotSlot =
    FastAutoAllocationRecordHotSlot::empty();

#[cfg(test)]
static FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS: AtomicUsize = AtomicUsize::new(0);

#[cfg(test)]
#[inline]
fn record_fast_auto_allocation_record_probe_step() {
    FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS.fetch_add(1, Ordering::Relaxed);
}

#[cfg(not(test))]
#[inline]
fn record_fast_auto_allocation_record_probe_step() {}

#[inline]
fn fast_auto_allocation_record_probe_already_checked(
    checked_hot_idx: Option<usize>,
    idx: usize,
) -> bool {
    checked_hot_idx == Some(idx)
}

struct MetadataScopeGuard {
    previous: AllocationMetadata,
}

impl Drop for MetadataScopeGuard {
    fn drop(&mut self) {
        unsafe {
            restore_active_metadata(self.previous);
        }
    }
}

struct AutoAllocationRecoveryRecordingGuard;

impl Drop for AutoAllocationRecoveryRecordingGuard {
    fn drop(&mut self) {
        unsafe {
            AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH =
                AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH.saturating_sub(1);
        }
    }
}

/// Run `f` while semantic allocations create pointer-to-metadata recovery
/// records for later ordinary `GlobalAlloc` dealloc/realloc calls.
///
/// Explicit Rust `SemanticAlloc` callers pass metadata again at deallocation
/// time and should not pay the recovery-record side-table cost. The ordinary
/// `GlobalAlloc` scoped/auto-metadata bridge and rustc-driver direct
/// allocator ABI are the paths that need recovery records after a compiler
/// scope exits, an auto allocation-site stream advances, or only the
/// allocation half of Box lowering was rewritten.
pub(crate) fn with_auto_allocation_recovery_recording<R>(f: impl FnOnce() -> R) -> R {
    unsafe {
        AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH =
            AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH.saturating_add(1);
    }
    let _guard = AutoAllocationRecoveryRecordingGuard;
    f()
}

#[inline]
fn auto_allocation_recovery_recording_enabled() -> bool {
    unsafe { AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH != 0 }
}

/// Return true when runtime layout auto-metadata is enabled.
///
/// Auto metadata is global process state used by evaluation harnesses that need
/// to run existing `GlobalAlloc` benchmarks without source-level wrappers. It
/// deliberately derives a semantic id from `Layout`, so it is non-claim-grade
/// unless paired with compiler-assigned type metadata evidence.
pub fn semantic_auto_metadata_enabled() -> bool {
    AUTO_METADATA_CONFIG.read().flags != 0
}

/// Return true when ordinary `GlobalAlloc` calls need semantic-side checks.
///
/// The default allocator path is intentionally cold for semantic features:
/// when no coverage accounting, process-wide auto metadata, or scoped compiler
/// metadata is active, `GlobalAlloc` can skip thread-local metadata probes and
/// auto-metadata synthesis entirely.
#[inline]
pub fn semantic_runtime_slow_path_enabled() -> bool {
    let global_flags = SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed);
    if global_flags & SLOW_PATH_SCOPED_METADATA_MASK != 0 && current_thread_scoped_metadata_active()
    {
        return true;
    }
    if FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed) != 0
        && current_thread_fast_auto_allocation_records_active()
    {
        return true;
    }
    let mut runtime_flags = global_flags & !SLOW_PATH_SCOPED_METADATA_MASK;
    if runtime_flags & SLOW_PATH_AUTO_METADATA != 0 && auto_metadata_allocations_exhausted() {
        runtime_flags &= !SLOW_PATH_AUTO_METADATA;
    }
    runtime_flags != 0
}

#[inline]
fn auto_metadata_allocations_exhausted() -> bool {
    let config = *AUTO_METADATA_CONFIG.read();
    auto_metadata_allocations_exhausted_for(config)
}

#[inline]
fn auto_metadata_allocations_exhausted_for(config: AutoMetadataConfig) -> bool {
    let exhausted = AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED.load(Ordering::Relaxed)
        && config.compiler_stream_enabled();
    if exhausted {
        semantic_slow_path_clear(SLOW_PATH_AUTO_METADATA);
    }
    exhausted
}

#[inline]
fn mark_auto_compiler_stream_exhausted() {
    AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED.store(true, Ordering::Relaxed);
    semantic_slow_path_clear(SLOW_PATH_AUTO_METADATA);
}

/// Return true when ordinary `GlobalAlloc::alloc` needs semantic-side work.
///
/// Outstanding allocation records are needed by later dealloc/realloc recovery,
/// but they do not require unrelated future allocations to probe thread-local
/// scopes, synthesize auto metadata, or record fallback coverage counters.
#[inline]
pub fn semantic_allocation_slow_path_enabled() -> bool {
    let global_flags = SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed);
    if global_flags & SLOW_PATH_SCOPED_METADATA_MASK != 0 && current_thread_scoped_metadata_active()
    {
        return true;
    }
    let mut allocation_flags =
        global_flags & !(SLOW_PATH_ALLOCATION_RECORDS | SLOW_PATH_SCOPED_METADATA_MASK);
    if allocation_flags & SLOW_PATH_AUTO_METADATA != 0 && auto_metadata_allocations_exhausted() {
        allocation_flags &= !SLOW_PATH_AUTO_METADATA;
    }
    allocation_flags != 0
}

#[inline]
fn semantic_slow_path_set(flag: usize) {
    SEMANTIC_SLOW_PATH_FLAGS.fetch_or(flag, Ordering::Relaxed);
}

#[inline]
fn semantic_slow_path_clear(flag: usize) {
    SEMANTIC_SLOW_PATH_FLAGS.fetch_and(!flag, Ordering::Relaxed);
}

#[inline]
fn scoped_metadata_is_active(metadata: AllocationMetadata) -> bool {
    metadata != AllocationMetadata::unknown()
}

#[inline]
fn current_thread_scoped_metadata_active() -> bool {
    unsafe { scoped_metadata_is_active(ACTIVE_METADATA) }
}

#[inline]
fn current_thread_fast_auto_allocation_records_active() -> bool {
    unsafe {
        FAST_AUTO_ALLOCATION_RECORD_COUNT != 0 || !FAST_AUTO_ALLOCATION_RECORD_INLINE.is_empty()
    }
}

fn scoped_metadata_activate() {
    if SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK == 0 {
        semantic_slow_path_set(SLOW_PATH_SCOPED_METADATA_UNIT);
    }
}

#[inline]
fn scoped_metadata_deactivate() {
    // The process-wide flag is only a coarse gate telling ordinary GlobalAlloc
    // calls that scoped metadata has been used in this process.  Exact activity
    // is thread-local (`ACTIVE_METADATA`), checked by
    // `current_thread_scoped_metadata_active()`.  Keeping this bit sticky avoids
    // an atomic decrement/CAS on every compiler-inserted scope pop; unrelated
    // threads still return false from the slow-path predicate because their TLS
    // active metadata is empty.
}

#[inline]
fn fast_auto_allocation_record_global_activate() {
    FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.fetch_add(1, Ordering::Relaxed);
}

#[inline]
fn fast_auto_allocation_record_global_deactivate() {
    let mut count = FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed);
    loop {
        if count == 0 {
            return;
        }
        match FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.compare_exchange_weak(
            count,
            count - 1,
            Ordering::Relaxed,
            Ordering::Relaxed,
        ) {
            Ok(_) => return,
            Err(next) => count = next,
        }
    }
}

#[inline]
unsafe fn replace_active_metadata(metadata: AllocationMetadata) -> AllocationMetadata {
    let previous = ACTIVE_METADATA;
    let previous_active = scoped_metadata_is_active(previous);
    let next_active = scoped_metadata_is_active(metadata);
    if previous_active != next_active {
        if next_active {
            scoped_metadata_activate();
        } else {
            scoped_metadata_deactivate();
        }
    }
    ACTIVE_METADATA = metadata;
    previous
}

#[inline(always)]
unsafe fn set_semantic_scope_active_metadata(metadata: AllocationMetadata) {
    if scoped_metadata_is_active(metadata) && !scoped_metadata_is_active(ACTIVE_METADATA) {
        scoped_metadata_activate();
    }
    ACTIVE_METADATA = metadata;
}

fn normalize_auto_metadata_flags(flags: u32) -> u32 {
    if flags == 0 {
        FLAG_TYPE_ISOLATED
    } else {
        flags | FLAG_TYPE_ISOLATED
    }
}

#[inline]
unsafe fn clear_auto_layout_metadata_hot_slot() {
    AUTO_LAYOUT_METADATA_HOT_SLOT = AutoLayoutMetadataHotSlot::empty();
}

#[inline]
fn synthesize_layout_auto_metadata(
    layout: Layout,
    flags: u32,
    module_id: u64,
    callsite: u64,
) -> AllocationMetadata {
    AllocationMetadata {
        type_id: semantic_layout_id(layout.size(), layout.align()),
        module_id,
        flags,
        lifetime_hint: 0,
        placement_hint: LAYOUT_DERIVED_PLACEMENT_HINT,
        callsite,
    }
}

#[inline]
fn layout_auto_metadata(
    layout: Layout,
    flags: u32,
    module_id: u64,
    callsite: u64,
) -> AllocationMetadata {
    unsafe {
        let cached = AUTO_LAYOUT_METADATA_HOT_SLOT;
        if cached.matches(layout, flags, module_id, callsite) {
            return cached.metadata;
        }
        let metadata = synthesize_layout_auto_metadata(layout, flags, module_id, callsite);
        AUTO_LAYOUT_METADATA_HOT_SLOT = AutoLayoutMetadataHotSlot {
            size: layout.size(),
            align: layout.align(),
            flags,
            module_id,
            callsite,
            metadata,
        };
        metadata
    }
}

/// Return true when process-wide auto metadata is replaying compiler-provided
/// allocation-site ids rather than synthesizing ids from layout alone.
pub fn semantic_auto_compiler_metadata_enabled() -> bool {
    AUTO_METADATA_CONFIG.read().compiler_metadata_enabled()
}

/// Return true when compiler-provided ids are consumed once without wrapping.
pub fn semantic_auto_compiler_metadata_stream_enabled() -> bool {
    AUTO_METADATA_CONFIG.read().compiler_stream_enabled()
}

/// Identify the current process-wide auto metadata type-id basis for emitted
/// evaluation events.
pub fn semantic_auto_metadata_type_id_basis() -> &'static str {
    let config = *AUTO_METADATA_CONFIG.read();
    if config.flags == 0 {
        "none"
    } else if config.compiler_stream_enabled() {
        "compiler-assigned-allocation-site-object-type-id-consuming-stream"
    } else if config.compiler_metadata_enabled() {
        "compiler-assigned-allocation-site-object-type-id-cyclic-replay"
    } else {
        "layout-derived-size-align"
    }
}

/// Enable process-wide layout auto-metadata for ordinary `GlobalAlloc` calls.
///
/// `flags == 0` selects `FLAG_TYPE_ISOLATED`; otherwise the supplied flags are
/// ORed with `FLAG_TYPE_ISOLATED` so the typed frontend is exercised. Scoped
/// metadata on the current thread still takes precedence over this mode.
pub fn semantic_auto_metadata_enable(module_id: u64, flags: u32, callsite: u64) {
    let normalized_flags = normalize_auto_metadata_flags(flags);
    let mut config = AUTO_METADATA_CONFIG.write();
    clear_auto_allocation_records();
    unsafe {
        clear_auto_layout_metadata_hot_slot();
        AUTO_COMPILER_TYPE_IDS_TLS_CURSOR = 0;
    }
    semantic_slow_path_set(SLOW_PATH_AUTO_METADATA);
    AUTO_COMPILER_TYPE_IDS_CURSOR.store(0, Ordering::Relaxed);
    AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED.store(false, Ordering::Relaxed);
    *config = AutoMetadataConfig {
        flags: normalized_flags,
        module_id,
        callsite,
        ..AutoMetadataConfig::disabled()
    };
}

unsafe fn semantic_auto_compiler_metadata_enable_with_mode(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
    mode: usize,
    global_recovery: bool,
) -> bool {
    if type_ids.is_null() || len == 0 {
        return false;
    }
    let normalized_flags = normalize_auto_metadata_flags(flags);
    let mut config = AUTO_METADATA_CONFIG.write();
    clear_auto_allocation_records();
    clear_auto_layout_metadata_hot_slot();
    semantic_slow_path_set(SLOW_PATH_AUTO_METADATA);
    AUTO_COMPILER_TYPE_IDS_CURSOR.store(0, Ordering::Relaxed);
    AUTO_COMPILER_TYPE_IDS_TLS_CURSOR = 0;
    AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED.store(false, Ordering::Relaxed);
    *config = AutoMetadataConfig {
        flags: normalized_flags,
        module_id,
        callsite,
        compiler_type_ids_ptr: type_ids as usize,
        compiler_type_ids_len: len,
        compiler_type_ids_mode: mode,
        compiler_type_ids_global_recovery: global_recovery,
    };
    true
}

/// Enable process-wide auto metadata by replaying compiler-provided
/// allocation-site type ids for ordinary `GlobalAlloc` calls.
///
/// This bridge is intentionally labeled as cyclic replay: it validates that
/// unmodified benchmark allocations can flow through the same runtime ABI with
/// compiler-assigned ids, but it is not by itself proof that a compiler pass
/// dynamically attributed each runtime allocation to its exact site.
///
/// # Safety
///
/// `type_ids` must point to `len` valid `u64` entries that remain alive until
/// `semantic_auto_metadata_disable` is called or a different auto metadata mode
/// is enabled. The caller is also responsible for avoiding concurrent mutation
/// of that slice while allocation may occur.
pub unsafe fn semantic_auto_compiler_metadata_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
) -> bool {
    semantic_auto_compiler_metadata_enable_with_mode(
        module_id,
        flags,
        callsite,
        type_ids,
        len,
        AUTO_COMPILER_TYPE_IDS_MODE_CYCLIC_REPLAY,
        true,
    )
}

/// Enable cyclic compiler-site replay with same-thread recovery records.
///
/// This mode is intended for single-threaded benchmark/evaluation harnesses.
/// Exact metadata recovery stays in the TLS fast table instead of the sharded
/// global table, so the ordinary `GlobalAlloc` hot path avoids a global lock.
/// Use `semantic_auto_compiler_metadata_enable` for conservative cross-thread
/// visibility.
///
/// # Safety
///
/// `type_ids` must satisfy the same lifetime contract as
/// `semantic_auto_compiler_metadata_enable`.  Pointers allocated in this mode
/// must be deallocated/reallocated on the allocating thread unless another ABI
/// path supplies exact deallocation metadata.
pub unsafe fn semantic_auto_compiler_metadata_thread_local_recovery_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
) -> bool {
    semantic_auto_compiler_metadata_enable_with_mode(
        module_id,
        flags,
        callsite,
        type_ids,
        len,
        AUTO_COMPILER_TYPE_IDS_MODE_CYCLIC_REPLAY,
        false,
    )
}

/// Enable process-wide auto metadata by consuming compiler-provided
/// allocation-site type ids exactly once for ordinary `GlobalAlloc` calls.
///
/// Unlike cyclic replay, this mode never wraps. After the finite stream is
/// exhausted, additional ordinary allocations are left untyped so coverage
/// evidence can expose a short runtime id stream rather than hiding it behind
/// layout-derived fallback ids.
///
/// # Safety
///
/// `type_ids` must point to `len` valid `u64` entries that remain alive until
/// `semantic_auto_metadata_disable` is called or a different auto metadata mode
/// is enabled. The caller is also responsible for avoiding concurrent mutation
/// of that slice while allocation may occur.
pub unsafe fn semantic_auto_compiler_metadata_stream_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
) -> bool {
    semantic_auto_compiler_metadata_enable_with_mode(
        module_id,
        flags,
        callsite,
        type_ids,
        len,
        AUTO_COMPILER_TYPE_IDS_MODE_CONSUMING_STREAM,
        true,
    )
}

/// Enable finite compiler-site replay with same-thread recovery records.
///
/// This keeps exact metadata recovery in the TLS fast table rather than the
/// process-global table.  It is appropriate for local benchmark replay and for
/// compiler paths that prove same-thread deallocation; the default stream API
/// remains conservative for unknown cross-thread ownership transfer.
///
/// # Safety
///
/// `type_ids` must satisfy the same lifetime contract as
/// `semantic_auto_compiler_metadata_stream_enable`.  Pointers allocated in this
/// mode must be deallocated/reallocated on the allocating thread unless exact
/// metadata is supplied by another path.
pub unsafe fn semantic_auto_compiler_metadata_stream_thread_local_recovery_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
) -> bool {
    semantic_auto_compiler_metadata_enable_with_mode(
        module_id,
        flags,
        callsite,
        type_ids,
        len,
        AUTO_COMPILER_TYPE_IDS_MODE_CONSUMING_STREAM,
        false,
    )
}

/// Disable process-wide layout auto-metadata for ordinary `GlobalAlloc` calls.
pub fn semantic_auto_metadata_disable() {
    let mut config = AUTO_METADATA_CONFIG.write();
    clear_auto_allocation_records();
    unsafe {
        clear_auto_layout_metadata_hot_slot();
        AUTO_COMPILER_TYPE_IDS_TLS_CURSOR = 0;
    }
    AUTO_COMPILER_TYPE_IDS_CURSOR.store(0, Ordering::Relaxed);
    AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED.store(false, Ordering::Relaxed);
    *config = AutoMetadataConfig::disabled();
    semantic_slow_path_clear(SLOW_PATH_AUTO_METADATA);
}

#[inline]
fn next_auto_compiler_type_id_index(
    len: usize,
    stream_mode: bool,
    global_recovery: bool,
) -> Option<usize> {
    if !stream_mode {
        if !global_recovery {
            // Thread-local compiler replay is used by single-thread benchmark
            // validation and by compiler paths that prove same-thread drop.
            // It does not require a process-global allocation-site order, so a
            // TLS cursor avoids a global atomic fetch_add on every allocation.
            unsafe {
                let idx = AUTO_COMPILER_TYPE_IDS_TLS_CURSOR % len;
                AUTO_COMPILER_TYPE_IDS_TLS_CURSOR = (idx + 1) % len;
                return Some(idx);
            }
        }
        return Some(AUTO_COMPILER_TYPE_IDS_CURSOR.fetch_add(1, Ordering::Relaxed) % len);
    }

    if AUTO_COMPILER_TYPE_IDS_STREAM_EXHAUSTED.load(Ordering::Relaxed) {
        return None;
    }

    let mut cursor = AUTO_COMPILER_TYPE_IDS_CURSOR.load(Ordering::Relaxed);
    loop {
        if cursor >= len {
            mark_auto_compiler_stream_exhausted();
            return None;
        }
        match AUTO_COMPILER_TYPE_IDS_CURSOR.compare_exchange_weak(
            cursor,
            cursor + 1,
            Ordering::Relaxed,
            Ordering::Relaxed,
        ) {
            Ok(_) => {
                if cursor + 1 == len {
                    mark_auto_compiler_stream_exhausted();
                }
                return Some(cursor);
            }
            Err(next) => cursor = next,
        }
    }
}

/// Synthesize evaluation-only metadata for an unscoped `GlobalAlloc` layout.
pub fn auto_allocation_metadata(layout: Layout) -> Option<AllocationMetadata> {
    let config = AUTO_METADATA_CONFIG.read();
    if config.flags == 0 {
        return None;
    }
    if config.compiler_metadata_enabled() {
        let stream_mode = config.compiler_stream_enabled();
        let idx = next_auto_compiler_type_id_index(
            config.compiler_type_ids_len,
            stream_mode,
            config.compiler_type_ids_global_recovery,
        )?;
        // The read guard intentionally remains live through this dereference.
        // Disable/reconfigure cannot acquire the write side and return control
        // to a caller that may free the old slice until this load completes.
        let type_id = unsafe { *((config.compiler_type_ids_ptr as *const u64).add(idx)) };
        if type_id != UNKNOWN_SEMANTIC_ID {
            return Some(AllocationMetadata {
                type_id,
                module_id: config.module_id,
                flags: config.flags,
                lifetime_hint: 0,
                placement_hint: 0,
                callsite: config.callsite.wrapping_add(idx as u64),
            });
        }
        if stream_mode {
            return None;
        }
    }
    Some(layout_auto_metadata(
        layout,
        config.flags,
        config.module_id,
        config.callsite,
    ))
}

/// Synthesize metadata for an unscoped `GlobalAlloc::dealloc` without
/// advancing allocation-site ID streams.
///
/// Allocation-site streams model allocation events.  A plain `GlobalAlloc`
/// deallocation does not carry enough information to recover the exact
/// compiler-emitted allocation-site id, and consuming the next stream entry at
/// deallocation time would shift all later allocation events to the wrong
/// compiler site.  Keep policy flags available for zeroing/quarantine-style
/// experiments, but mark compiler-stream deallocations as fallback metadata
/// unless a real scoped/compiler deallocation metadata path is active.
pub fn auto_deallocation_metadata(layout: Layout) -> Option<AllocationMetadata> {
    let config = *AUTO_METADATA_CONFIG.read();
    if config.flags == 0 {
        return None;
    }

    if config.compiler_metadata_enabled() {
        if auto_metadata_allocations_exhausted_for(config) {
            return None;
        }
        return Some(AllocationMetadata {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: config.module_id,
            flags: config.flags,
            lifetime_hint: 0,
            placement_hint: 0,
            callsite: config.callsite,
        });
    }

    Some(layout_auto_metadata(
        layout,
        config.flags,
        config.module_id,
        config.callsite,
    ))
}

#[inline]
fn auto_allocation_recording_required(metadata: AllocationMetadata) -> bool {
    metadata.has_type() && !metadata.is_layout_derived()
}

#[inline]
pub(crate) fn active_allocation_metadata_requires_recovery_record(
    metadata: AllocationMetadata,
) -> bool {
    metadata.placement_hint & PLACEMENT_HINT_CROSS_THREAD_RECOVERY != 0
        || metadata.placement_hint & PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY == 0
}

#[inline]
fn auto_allocation_record_eligible(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    let ptr_key = ptr as usize;
    !ptr.is_null()
        && ptr_key != AUTO_ALLOCATION_RECORD_TOMBSTONE_PTR
        && layout.size() != 0
        && auto_allocation_recording_required(metadata)
}

#[inline]
fn auto_allocation_record_requires_global_visibility(metadata: AllocationMetadata) -> bool {
    // Stats collection reads allocation/deallocation events on the same slow
    // path that records or consumes recovery metadata; it does not require the
    // pointer-to-metadata record to be process-visible.  Keep ordinary
    // same-thread compiler recovery in the TLS fast table so claim probes can
    // gather stats without paying the global mutex/inline-table footprint.
    //
    // Process-wide compiler replay defaults to global visibility because the
    // matching deallocation may happen on a different thread after the stream
    // cursor has advanced.  Benchmark harnesses that prove same-thread replay
    // can opt into TLS recovery so every allocation does not take the global
    // recovery-table lock.
    if metadata.placement_hint & PLACEMENT_HINT_CROSS_THREAD_RECOVERY != 0 {
        return true;
    }
    let config = *AUTO_METADATA_CONFIG.read();
    config.compiler_metadata_enabled() && config.compiler_type_ids_global_recovery
}

#[inline]
fn auto_allocation_record_slot(ptr: *mut u8) -> usize {
    let mut value = (ptr as usize) >> 4;
    value ^= value >> 17;
    value ^= value >> 33;
    value.wrapping_mul(0x517c_c1b7usize) & (AUTO_ALLOCATION_RECORD_SLOTS - 1)
}

#[inline]
fn auto_allocation_record_shard_and_slot(ptr: *mut u8) -> (usize, usize) {
    let slot = auto_allocation_record_slot(ptr);
    (
        slot / AUTO_ALLOCATION_RECORD_SHARD_SLOTS,
        slot & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1),
    )
}

#[inline]
fn auto_allocation_record_shard(ptr: *mut u8) -> usize {
    auto_allocation_record_shard_and_slot(ptr).0
}

#[inline]
fn next_auto_allocation_record_shard(home_shard_idx: usize, shard_offset: usize) -> usize {
    (home_shard_idx + shard_offset) & AUTO_ALLOCATION_RECORD_SHARD_MASK
}

#[inline]
fn auto_allocation_record_shard_bit(shard_idx: usize) -> usize {
    1usize << shard_idx
}

#[inline]
fn auto_allocation_record_active_shards() -> usize {
    AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.load(Ordering::Acquire)
}

#[inline]
fn auto_allocation_record_shard_is_active(active_mask: usize, shard_idx: usize) -> bool {
    active_mask & auto_allocation_record_shard_bit(shard_idx) != 0
}

#[inline]
fn auto_allocation_record_all_shards_mask() -> usize {
    (1usize << AUTO_ALLOCATION_RECORD_SHARD_COUNT) - 1
}

#[inline]
fn auto_allocation_record_active_offsets_from_home(
    active_mask: usize,
    home_shard_idx: usize,
) -> usize {
    let active_mask = active_mask & auto_allocation_record_all_shards_mask();
    if home_shard_idx == 0 {
        active_mask
    } else {
        ((active_mask >> home_shard_idx)
            | (active_mask << (AUTO_ALLOCATION_RECORD_SHARD_COUNT - home_shard_idx)))
            & auto_allocation_record_all_shards_mask()
    }
}

#[inline]
fn auto_allocation_record_probe_offsets_including_home(
    active_mask: usize,
    home_shard_idx: usize,
) -> usize {
    if active_mask == 0 {
        auto_allocation_record_all_shards_mask()
    } else {
        // The active-shard mask is a lock-skipping hint, not an authority for
        // where the pointer's home slot may be.  Include offset zero even if a
        // stale mask forgot the home bit; insertion already uses this policy,
        // and recovery must not be weaker than recording.
        auto_allocation_record_active_offsets_from_home(active_mask, home_shard_idx) | 1usize
    }
}

#[inline]
fn pop_next_auto_allocation_record_active_shard(
    home_shard_idx: usize,
    active_offsets: &mut usize,
) -> Option<(usize, usize)> {
    if *active_offsets == 0 {
        return None;
    }
    let shard_offset = active_offsets.trailing_zeros() as usize;
    *active_offsets &= *active_offsets - 1;
    Some((
        next_auto_allocation_record_shard(home_shard_idx, shard_offset),
        shard_offset,
    ))
}

#[inline]
fn auto_allocation_record_mark_shard_active(shard_idx: usize) {
    AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.fetch_or(
        auto_allocation_record_shard_bit(shard_idx),
        Ordering::Release,
    );
}

#[inline]
fn auto_allocation_record_clear_shard_active(shard_idx: usize) {
    AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.fetch_and(
        !auto_allocation_record_shard_bit(shard_idx),
        Ordering::Release,
    );
}

#[inline]
fn fast_auto_allocation_record_slot(ptr: *mut u8) -> usize {
    let mut value = (ptr as usize) >> 4;
    value ^= value >> 16;
    value ^= value >> 32;
    value.wrapping_mul(0x9e37_79b1usize) & (FAST_AUTO_ALLOCATION_RECORD_SLOTS - 1)
}

#[inline]
fn fast_auto_allocation_record_auth(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> u64 {
    if metadata_integrity_required(metadata) {
        derive_auto_allocation_record_auth(ptr, layout, metadata)
    } else {
        0
    }
}

#[inline]
fn fast_auto_allocation_record_auth_matches(
    ptr: *mut u8,
    layout: Layout,
    record: AutoAllocationRecord,
) -> bool {
    if record.auth == 0 && !metadata_integrity_required(record.metadata) {
        true
    } else {
        record.auth == derive_auto_allocation_record_auth(ptr, layout, record.metadata)
    }
}

#[inline]
fn fast_auto_allocation_record_for(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> AutoAllocationRecord {
    AutoAllocationRecord {
        ptr: ptr as usize,
        size: layout.size(),
        align: layout.align(),
        auth: fast_auto_allocation_record_auth(ptr, layout, metadata),
        metadata,
    }
}

#[inline]
fn auto_allocation_record_for(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> AutoAllocationRecord {
    AutoAllocationRecord {
        ptr: ptr as usize,
        size: layout.size(),
        align: layout.align(),
        auth: derive_auto_allocation_record_auth(ptr, layout, metadata),
        metadata,
    }
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn allocate_auto_allocation_record_page() -> *mut AutoAllocationRecordPage {
    let prot = system_alloc::prots::get_prot(true, true, false);
    let page = system_alloc::mmap(size_of::<AutoAllocationRecordPage>(), prot)
        as *mut AutoAllocationRecordPage;
    if page.is_null() || page as usize == usize::MAX {
        return core::ptr::null_mut();
    }
    page.write(AutoAllocationRecordPage::empty());
    page
}

#[cfg(not(feature = "fixed_heap"))]
fn find_auto_allocation_record_in_overflow(
    overflow: *mut AutoAllocationRecordPage,
    ptr_key: usize,
) -> Option<*mut AutoAllocationRecord> {
    let mut page = overflow;
    while !page.is_null() {
        let page_ref = unsafe { &mut *page };
        let mut entry_idx = 0;
        while entry_idx < AUTO_ALLOCATION_RECORD_OVERFLOW_SLOTS {
            let entry = &mut page_ref.entries[entry_idx];
            if entry.ptr == ptr_key {
                return Some(entry as *mut AutoAllocationRecord);
            }
            entry_idx += 1;
        }
        page = page_ref.next;
    }
    None
}

#[cfg(not(feature = "fixed_heap"))]
fn first_available_auto_allocation_record_in_overflow(
    overflow: *mut AutoAllocationRecordPage,
) -> *mut AutoAllocationRecord {
    let mut page = overflow;
    while !page.is_null() {
        let page_ref = unsafe { &mut *page };
        let mut entry_idx = 0;
        while entry_idx < AUTO_ALLOCATION_RECORD_OVERFLOW_SLOTS {
            let entry = &mut page_ref.entries[entry_idx];
            if entry.is_available() {
                return entry as *mut AutoAllocationRecord;
            }
            entry_idx += 1;
        }
        page = page_ref.next;
    }
    core::ptr::null_mut()
}

#[cfg(not(feature = "fixed_heap"))]
fn auto_allocation_record_overflow_page_has_live_record(page: &AutoAllocationRecordPage) -> bool {
    page.entries.iter().any(|entry| !entry.is_available())
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn release_empty_auto_allocation_record_overflow_pages(
    head: &mut *mut AutoAllocationRecordPage,
) {
    let mut prev: *mut AutoAllocationRecordPage = core::ptr::null_mut();
    let mut page = *head;

    while !page.is_null() {
        let next = (*page).next;
        // Overflow chains are full scans, not linear-probe chains.  Tombstones
        // only preserve reuse semantics inside the page, so a page with no live
        // records can be unlinked and unmapped immediately after the record that
        // made it empty is consumed.
        if !auto_allocation_record_overflow_page_has_live_record(&*page) {
            if prev.is_null() {
                *head = next;
            } else {
                (*prev).next = next;
            }
            system_alloc::munmap(page as *mut u8, size_of::<AutoAllocationRecordPage>());
        } else {
            prev = page;
        }
        page = next;
    }
}

fn clear_auto_allocation_records() {
    for table_lock in AUTO_ALLOCATION_RECORDS.iter() {
        let mut table = table_lock.lock();
        #[cfg(not(feature = "fixed_heap"))]
        {
            let mut page = table.overflow;
            while !page.is_null() {
                let next = unsafe { (*page).next };
                unsafe {
                    system_alloc::munmap(page as *mut u8, size_of::<AutoAllocationRecordPage>());
                }
                page = next;
            }
        }
        *table = GlobalAutoAllocationRecordTable::empty();
    }
    AUTO_ALLOCATION_RECORD_COUNT.store(0, Ordering::Relaxed);
    AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.store(0, Ordering::Release);
    unsafe {
        FAST_AUTO_ALLOCATION_RECORD_INLINE = AutoAllocationRecord::empty();
        FAST_AUTO_ALLOCATION_RECORDS =
            [AutoAllocationRecord::empty(); FAST_AUTO_ALLOCATION_RECORD_SLOTS];
        FAST_AUTO_ALLOCATION_RECORD_COUNT = 0;
        FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT = FastAutoAllocationRecordHotSlot::empty();
    }
    #[cfg(test)]
    FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS.store(0, Ordering::Relaxed);
    FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.store(0, Ordering::Relaxed);
    semantic_slow_path_clear(SLOW_PATH_ALLOCATION_RECORDS);
}

#[inline]
unsafe fn remember_fast_auto_allocation_record_hot_slot(ptr_key: usize, slot_idx: usize) {
    FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT = FastAutoAllocationRecordHotSlot {
        ptr: ptr_key,
        slot_idx,
    };
}

#[inline]
unsafe fn clear_fast_auto_allocation_record_hot_slot(ptr_key: usize) {
    if FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT.ptr == ptr_key {
        FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT = FastAutoAllocationRecordHotSlot::empty();
    }
}

#[inline]
unsafe fn fast_auto_allocation_record_hot_slot(ptr_key: usize) -> Option<usize> {
    let hot = FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT;
    if hot.ptr == ptr_key {
        Some(hot.slot_idx & (FAST_AUTO_ALLOCATION_RECORD_SLOTS - 1))
    } else {
        None
    }
}

#[inline]
fn remember_global_auto_allocation_record_hot_slot(
    table: &mut GlobalAutoAllocationRecordTable,
    ptr_key: usize,
    slot_idx: usize,
) {
    table.hot = GlobalAutoAllocationRecordHotSlot {
        ptr: ptr_key,
        slot_idx,
    };
}

#[inline]
fn clear_global_auto_allocation_record_hot_slot(
    table: &mut GlobalAutoAllocationRecordTable,
    ptr_key: usize,
) {
    if table.hot.ptr == ptr_key {
        table.hot = GlobalAutoAllocationRecordHotSlot::empty();
    }
}

#[inline]
fn global_auto_allocation_record_hot_slot(
    table: &GlobalAutoAllocationRecordTable,
    ptr_key: usize,
) -> Option<usize> {
    if table.hot.ptr == ptr_key {
        Some(table.hot.slot_idx & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1))
    } else {
        None
    }
}

fn refresh_global_auto_allocation_record_shard_active(
    shard_idx: usize,
    table: &GlobalAutoAllocationRecordTable,
) {
    if table.live_count != 0 {
        auto_allocation_record_mark_shard_active(shard_idx);
    } else {
        auto_allocation_record_clear_shard_active(shard_idx);
    }
}

#[inline]
fn increment_global_auto_allocation_record_shard_live_count(
    shard_idx: usize,
    table: &mut GlobalAutoAllocationRecordTable,
) {
    table.live_count = table.live_count.saturating_add(1);
    auto_allocation_record_mark_shard_active(shard_idx);
}

#[inline]
fn decrement_global_auto_allocation_record_shard_live_count(
    shard_idx: usize,
    table: &mut GlobalAutoAllocationRecordTable,
) {
    table.live_count = table.live_count.saturating_sub(1);
    refresh_global_auto_allocation_record_shard_active(shard_idx, table);
}

#[inline]
unsafe fn record_fast_auto_allocation_metadata_eligible(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    let ptr_key = ptr as usize;

    let inline_record = FAST_AUTO_ALLOCATION_RECORD_INLINE;
    if inline_record.is_empty() || inline_record.ptr == ptr_key {
        if inline_record.is_empty() {
            fast_auto_allocation_record_global_activate();
        }
        FAST_AUTO_ALLOCATION_RECORD_INLINE = fast_auto_allocation_record_for(ptr, layout, metadata);
        return true;
    }

    let mut checked_hot_idx = None;
    if let Some(idx) = fast_auto_allocation_record_hot_slot(ptr_key) {
        checked_hot_idx = Some(idx);
        record_fast_auto_allocation_record_probe_step();
        let record = FAST_AUTO_ALLOCATION_RECORDS[idx];
        if record.ptr == ptr_key || record.is_available() {
            if record.is_available() {
                FAST_AUTO_ALLOCATION_RECORD_COUNT =
                    FAST_AUTO_ALLOCATION_RECORD_COUNT.saturating_add(1);
                fast_auto_allocation_record_global_activate();
            }
            FAST_AUTO_ALLOCATION_RECORDS[idx] =
                fast_auto_allocation_record_for(ptr, layout, metadata);
            return true;
        }
        // The hot hint is pointer-specific.  If the hinted slot now contains a
        // different live record, keep probing for an available slot but do not
        // let future calls repeatedly pay this stale hot check.
        clear_fast_auto_allocation_record_hot_slot(ptr_key);
    }

    let start = fast_auto_allocation_record_slot(ptr);
    let mut first_available = None;
    let mut offset = 0;
    while offset < FAST_AUTO_ALLOCATION_RECORD_PROBE_LIMIT {
        let idx = (start + offset) & (FAST_AUTO_ALLOCATION_RECORD_SLOTS - 1);
        if fast_auto_allocation_record_probe_already_checked(checked_hot_idx, idx) {
            offset += 1;
            continue;
        }
        record_fast_auto_allocation_record_probe_step();
        let record = FAST_AUTO_ALLOCATION_RECORDS[idx];
        if record.ptr == ptr_key {
            FAST_AUTO_ALLOCATION_RECORDS[idx] =
                fast_auto_allocation_record_for(ptr, layout, metadata);
            remember_fast_auto_allocation_record_hot_slot(ptr_key, idx);
            return true;
        }
        if record.is_available() && first_available.is_none() {
            first_available = Some(idx);
            if record.is_empty() {
                break;
            }
        }
        offset += 1;
    }

    let idx = match first_available {
        Some(idx) => idx,
        None => return false,
    };
    if FAST_AUTO_ALLOCATION_RECORDS[idx].is_available() {
        FAST_AUTO_ALLOCATION_RECORD_COUNT = FAST_AUTO_ALLOCATION_RECORD_COUNT.saturating_add(1);
        fast_auto_allocation_record_global_activate();
    }
    FAST_AUTO_ALLOCATION_RECORDS[idx] = fast_auto_allocation_record_for(ptr, layout, metadata);
    remember_fast_auto_allocation_record_hot_slot(ptr_key, idx);
    true
}

fn recover_fast_auto_allocation_record_metadata(
    ptr: *mut u8,
    layout: Layout,
    remove: bool,
) -> Option<AllocationMetadata> {
    let ptr_key = ptr as usize;
    if ptr.is_null()
        || ptr_key == AUTO_ALLOCATION_RECORD_TOMBSTONE_PTR
        || layout.size() == 0
        || !current_thread_fast_auto_allocation_records_active()
    {
        return None;
    }

    unsafe {
        let inline_record = FAST_AUTO_ALLOCATION_RECORD_INLINE;
        if inline_record.ptr == ptr_key {
            if inline_record.size == layout.size()
                && inline_record.align == layout.align()
                && fast_auto_allocation_record_auth_matches(ptr, layout, inline_record)
            {
                if remove {
                    FAST_AUTO_ALLOCATION_RECORD_INLINE = AutoAllocationRecord::empty();
                    fast_auto_allocation_record_global_deactivate();
                }
                return Some(inline_record.metadata);
            }
            return None;
        }

        let mut checked_hot_idx = None;
        if let Some(idx) = fast_auto_allocation_record_hot_slot(ptr_key) {
            record_fast_auto_allocation_record_probe_step();
            let record = FAST_AUTO_ALLOCATION_RECORDS[idx];
            if record.ptr == ptr_key {
                if record.size == layout.size()
                    && record.align == layout.align()
                    && fast_auto_allocation_record_auth_matches(ptr, layout, record)
                {
                    if remove {
                        FAST_AUTO_ALLOCATION_RECORD_COUNT =
                            FAST_AUTO_ALLOCATION_RECORD_COUNT.saturating_sub(1);
                        fast_auto_allocation_record_global_deactivate();
                        FAST_AUTO_ALLOCATION_RECORDS[idx] =
                            if FAST_AUTO_ALLOCATION_RECORD_COUNT == 0 {
                                AutoAllocationRecord::empty()
                            } else {
                                AutoAllocationRecord::tombstone()
                            };
                    }
                    return Some(record.metadata);
                }
                return None;
            }
            // A stale non-empty/tombstone hot slot has already been inspected;
            // skip it in the bounded probe below.  If it is empty, preserve the
            // old empty-stop semantics by letting the normal probe see it.
            if !record.is_empty() {
                checked_hot_idx = Some(idx);
            }
            clear_fast_auto_allocation_record_hot_slot(ptr_key);
        }

        let start = fast_auto_allocation_record_slot(ptr);
        let mut offset = 0;
        while offset < FAST_AUTO_ALLOCATION_RECORD_PROBE_LIMIT {
            let idx = (start + offset) & (FAST_AUTO_ALLOCATION_RECORD_SLOTS - 1);
            if fast_auto_allocation_record_probe_already_checked(checked_hot_idx, idx) {
                offset += 1;
                continue;
            }
            record_fast_auto_allocation_record_probe_step();
            let record = FAST_AUTO_ALLOCATION_RECORDS[idx];
            if record.is_empty() {
                return None;
            }
            if record.ptr == ptr_key {
                if record.size == layout.size()
                    && record.align == layout.align()
                    && fast_auto_allocation_record_auth_matches(ptr, layout, record)
                {
                    if remove {
                        FAST_AUTO_ALLOCATION_RECORD_COUNT =
                            FAST_AUTO_ALLOCATION_RECORD_COUNT.saturating_sub(1);
                        fast_auto_allocation_record_global_deactivate();
                        // This TLS table is on the compiler-scoped alloc/dealloc hot path.  The
                        // previous implementation zeroed every TLS slot whenever the last
                        // outstanding scoped allocation was freed, which made a one-allocation
                        // std_bench leaf spend most of its time clearing bookkeeping memory.  We
                        // only need probe-chain tombstones while other live records remain; when
                        // the table becomes empty, this slot can be cleared in O(1).
                        FAST_AUTO_ALLOCATION_RECORDS[idx] =
                            if FAST_AUTO_ALLOCATION_RECORD_COUNT == 0 {
                                AutoAllocationRecord::empty()
                            } else {
                                AutoAllocationRecord::tombstone()
                            };
                    }
                    remember_fast_auto_allocation_record_hot_slot(ptr_key, idx);
                    return Some(record.metadata);
                }
                return None;
            }
            offset += 1;
        }
    }
    None
}

#[inline]
unsafe fn record_auto_allocation_metadata(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    if !auto_allocation_record_eligible(ptr, layout, metadata) {
        return true;
    }

    if auto_allocation_recovery_recording_enabled() {
        return record_recovery_auto_allocation_metadata_eligible(ptr, layout, metadata);
    }

    record_global_auto_allocation_metadata_eligible(ptr, layout, metadata)
}

#[inline]
unsafe fn record_recovery_auto_allocation_metadata_eligible(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    if auto_allocation_record_requires_global_visibility(metadata) {
        record_global_auto_allocation_metadata_eligible(ptr, layout, metadata)
    } else if !record_fast_auto_allocation_metadata_eligible(ptr, layout, metadata) {
        record_global_auto_allocation_metadata_eligible(ptr, layout, metadata)
    } else {
        true
    }
}

#[inline]
unsafe fn record_recovery_auto_allocation_metadata(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    if !auto_allocation_record_eligible(ptr, layout, metadata) {
        return true;
    }

    record_recovery_auto_allocation_metadata_eligible(ptr, layout, metadata)
}

#[inline]
fn install_global_auto_allocation_inline_record(
    shard_idx: usize,
    table: &mut GlobalAutoAllocationRecordTable,
    idx: usize,
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    let ptr_key = ptr as usize;
    let existing = table.inline[idx];
    if existing.ptr != ptr_key && !existing.is_available() {
        return false;
    }
    if existing.is_available() {
        AUTO_ALLOCATION_RECORD_COUNT.fetch_add(1, Ordering::Relaxed);
        increment_global_auto_allocation_record_shard_live_count(shard_idx, table);
    } else {
        auto_allocation_record_mark_shard_active(shard_idx);
    }
    table.inline[idx] = auto_allocation_record_for(ptr, layout, metadata);
    remember_global_auto_allocation_record_hot_slot(table, ptr_key, idx);
    semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
    true
}

fn update_global_auto_allocation_record_in_shard(
    shard_idx: usize,
    table: &mut GlobalAutoAllocationRecordTable,
    start: usize,
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    search_overflow: bool,
    first_available: &mut Option<(usize, usize)>,
) -> bool {
    #[cfg(feature = "fixed_heap")]
    let _ = search_overflow;

    let ptr_key = ptr as usize;
    if let Some(idx) = global_auto_allocation_record_hot_slot(table, ptr_key) {
        let record = table.inline[idx];
        if record.ptr == ptr_key {
            auto_allocation_record_mark_shard_active(shard_idx);
            table.inline[idx] = auto_allocation_record_for(ptr, layout, metadata);
            remember_global_auto_allocation_record_hot_slot(table, ptr_key, idx);
            semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
            return true;
        }
        if record.is_available() && first_available.is_none() {
            *first_available = Some((shard_idx, idx));
        } else {
            clear_global_auto_allocation_record_hot_slot(table, ptr_key);
        }
    }

    let mut offset = 0;
    while offset < AUTO_ALLOCATION_RECORD_PROBE_LIMIT {
        let idx = (start + offset) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1);
        let record = table.inline[idx];
        if record.ptr == ptr_key {
            auto_allocation_record_mark_shard_active(shard_idx);
            table.inline[idx] = auto_allocation_record_for(ptr, layout, metadata);
            remember_global_auto_allocation_record_hot_slot(table, ptr_key, idx);
            semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
            return true;
        }
        if record.is_available() {
            if first_available.is_none() {
                *first_available = Some((shard_idx, idx));
            }
            if record.is_empty() {
                break;
            }
        }
        offset += 1;
    }

    #[cfg(feature = "fixed_heap")]
    {
        // fixed_heap builds do not have mmap-backed overflow pages.  Scan the
        // rest of this shard after the bounded hot probe, then continue to
        // later shards so total inline capacity remains 4096 records.
        let mut slow_offset = AUTO_ALLOCATION_RECORD_PROBE_LIMIT;
        while slow_offset < AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
            let idx = (start + slow_offset) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1);
            let record = table.inline[idx];
            if record.ptr == ptr_key {
                auto_allocation_record_mark_shard_active(shard_idx);
                table.inline[idx] = auto_allocation_record_for(ptr, layout, metadata);
                remember_global_auto_allocation_record_hot_slot(table, ptr_key, idx);
                semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
                return true;
            }
            if record.is_available() {
                if first_available.is_none() {
                    *first_available = Some((shard_idx, idx));
                }
                if record.is_empty() {
                    break;
                }
            }
            slow_offset += 1;
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        // Overflow pages are appended only to the pointer's home shard after
        // every shard-local inline table is full.  Inline records may live in a
        // different shard to preserve the historical global inline capacity,
        // but overflow records do not migrate.  Skipping non-home overflow
        // chains avoids scanning unrelated cold pages under their shard locks
        // and prevents a corrupt impossible non-home overflow entry from
        // shadowing the authoritative home-shard record.
        if search_overflow {
            if let Some(slot) = find_auto_allocation_record_in_overflow(table.overflow, ptr_key) {
                auto_allocation_record_mark_shard_active(shard_idx);
                unsafe {
                    *slot = auto_allocation_record_for(ptr, layout, metadata);
                }
                semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
                return true;
            }
        }
    }

    false
}

#[inline]
unsafe fn record_global_auto_allocation_metadata_eligible(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    let (home_shard_idx, home_start) = auto_allocation_record_shard_and_slot(ptr);
    let active_mask = auto_allocation_record_active_shards();
    let use_active_mask = active_mask != 0;
    let mut first_available: Option<(usize, usize)> = None;
    let active_offsets =
        auto_allocation_record_active_offsets_from_home(active_mask, home_shard_idx);
    let first_inactive: Option<(usize, usize)> = if use_active_mask {
        let inactive_offsets =
            (!active_offsets) & auto_allocation_record_all_shards_mask() & !1usize;
        if inactive_offsets != 0 {
            let shard_offset = inactive_offsets.trailing_zeros() as usize;
            let shard_idx = next_auto_allocation_record_shard(home_shard_idx, shard_offset);
            Some((shard_idx, 0))
        } else {
            None
        }
    } else {
        None
    };

    // Search every shard before inserting into a reusable slot.  That preserves
    // the old "replace, don't shadow" rule for in-place realloc/replay while
    // avoiding one process-wide mutex for unrelated cross-thread records.
    if use_active_mask {
        let mut search_offsets =
            auto_allocation_record_probe_offsets_including_home(active_mask, home_shard_idx);
        while let Some((shard_idx, shard_offset)) =
            pop_next_auto_allocation_record_active_shard(home_shard_idx, &mut search_offsets)
        {
            let start = if shard_offset == 0 { home_start } else { 0 };
            let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            if update_global_auto_allocation_record_in_shard(
                shard_idx,
                &mut *table,
                start,
                ptr,
                layout,
                metadata,
                shard_idx == home_shard_idx,
                &mut first_available,
            ) {
                return true;
            }
        }
    } else {
        let mut shard_offset = 0;
        while shard_offset < AUTO_ALLOCATION_RECORD_SHARD_COUNT {
            let shard_idx = next_auto_allocation_record_shard(home_shard_idx, shard_offset);
            let start = if shard_offset == 0 { home_start } else { 0 };
            let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            if update_global_auto_allocation_record_in_shard(
                shard_idx,
                &mut *table,
                start,
                ptr,
                layout,
                metadata,
                shard_idx == home_shard_idx,
                &mut first_available,
            ) {
                return true;
            }
            shard_offset += 1;
        }
    }

    if let Some((shard_idx, bucket_idx)) = first_available {
        let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
        if install_global_auto_allocation_inline_record(
            shard_idx,
            &mut *table,
            bucket_idx,
            ptr,
            layout,
            metadata,
        ) {
            return true;
        }
    }

    if let Some((shard_idx, start)) = first_inactive {
        let ptr_key = ptr as usize;
        let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
        let mut offset = 0;
        while offset < AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
            let idx = (start + offset) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1);
            let record = table.inline[idx];
            if record.ptr == ptr_key {
                auto_allocation_record_mark_shard_active(shard_idx);
                table.inline[idx] = auto_allocation_record_for(ptr, layout, metadata);
                remember_global_auto_allocation_record_hot_slot(&mut *table, ptr_key, idx);
                semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
                return true;
            }
            if record.is_available()
                && install_global_auto_allocation_inline_record(
                    shard_idx,
                    &mut *table,
                    idx,
                    ptr,
                    layout,
                    metadata,
                )
            {
                return true;
            }
            offset += 1;
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        // All shard-local inline probe windows were occupied.  Spill into the
        // home shard's overflow list; lookup scans each shard's overflow list
        // under the corresponding shard lock.
        let mut table = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
        let mut overflow_slot = first_available_auto_allocation_record_in_overflow(table.overflow);
        if overflow_slot.is_null() {
            let page = allocate_auto_allocation_record_page();
            if !page.is_null() {
                (*page).next = table.overflow;
                table.overflow = page;
                overflow_slot = &mut (*page).entries[0] as *mut AutoAllocationRecord;
            }
        }
        if !overflow_slot.is_null() {
            increment_global_auto_allocation_record_shard_live_count(home_shard_idx, &mut *table);
            *overflow_slot = auto_allocation_record_for(ptr, layout, metadata);
            AUTO_ALLOCATION_RECORD_COUNT.fetch_add(1, Ordering::Relaxed);
            semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
            return true;
        }
    }
    false
}

#[inline]
unsafe fn record_global_auto_allocation_metadata(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    if !auto_allocation_record_eligible(ptr, layout, metadata) {
        return true;
    }

    record_global_auto_allocation_metadata_eligible(ptr, layout, metadata)
}

fn recover_auto_allocation_record_metadata(
    ptr: *mut u8,
    layout: Layout,
    remove: bool,
) -> Option<AllocationMetadata> {
    if let Some(metadata) = recover_fast_auto_allocation_record_metadata(ptr, layout, remove) {
        return Some(metadata);
    }

    recover_global_auto_allocation_record_metadata(ptr, layout, remove)
}

fn take_auto_allocation_records_for_reallocation(
    ptr: *mut u8,
    layout: Layout,
) -> Option<AllocationMetadata> {
    // In-place realloc changes the allocator-visible layout while preserving
    // the pointer.  The old recovery record must be replaced, not shadowed:
    // visibility can change between the TLS fast table and the process-global
    // table when stats/compiler-stream/cross-thread gates change.  Probe both
    // tables explicitly so a stale record in one table cannot outlive the new
    // record in the other table.
    let fast_metadata = recover_fast_auto_allocation_record_metadata(ptr, layout, true);
    let global_metadata = recover_global_auto_allocation_record_metadata(ptr, layout, true);
    fast_metadata.or(global_metadata)
}

pub(crate) fn take_recorded_reallocation_old_metadata(
    ptr: *mut u8,
    layout: Layout,
) -> Option<AllocationMetadata> {
    take_auto_allocation_records_for_reallocation(ptr, layout)
}

unsafe fn replace_auto_allocation_record_for_reallocation(
    ptr: *mut u8,
    old_layout: Layout,
    new_layout: Layout,
    new_metadata: AllocationMetadata,
) -> bool {
    let old_metadata = take_auto_allocation_records_for_reallocation(ptr, old_layout);
    if record_recovery_auto_allocation_metadata(ptr, new_layout, new_metadata) {
        return true;
    }

    if let Some(old_metadata) = old_metadata {
        let _ = record_recovery_auto_allocation_metadata(ptr, old_layout, old_metadata);
    }
    false
}

fn remove_global_auto_allocation_inline_record(
    shard_idx: usize,
    table: &mut GlobalAutoAllocationRecordTable,
    idx: usize,
    ptr_key: usize,
) {
    clear_global_auto_allocation_record_hot_slot(table, ptr_key);
    if atomic_saturating_decrement(&AUTO_ALLOCATION_RECORD_COUNT) == 0 {
        // This global recovery table is on the compiler-scoped GlobalAlloc
        // deallocation path.  The TLS fast table already avoids clearing all
        // slots when the last live record is removed; keep the same O(1)
        // behavior here. Tombstones are only needed while another live record
        // may depend on the probe chain, so the final removed slot can become
        // an empty stop marker and the slow-path flag can be cleared
        // immediately.
        table.inline[idx] = AutoAllocationRecord::empty();
        table.live_count = table.live_count.saturating_sub(1);
        AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.store(0, Ordering::Release);
        semantic_slow_path_clear(SLOW_PATH_ALLOCATION_RECORDS);
    } else {
        table.inline[idx] = AutoAllocationRecord::tombstone();
        decrement_global_auto_allocation_record_shard_live_count(shard_idx, table);
    }
}

fn recover_global_auto_allocation_record_metadata(
    ptr: *mut u8,
    layout: Layout,
    remove: bool,
) -> Option<AllocationMetadata> {
    let ptr_key = ptr as usize;
    if ptr.is_null()
        || ptr_key == AUTO_ALLOCATION_RECORD_TOMBSTONE_PTR
        || layout.size() == 0
        || AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed) == 0
    {
        return None;
    }

    let (home_shard_idx, home_start) = auto_allocation_record_shard_and_slot(ptr);
    let active_mask = auto_allocation_record_active_shards();
    // The active-shard bitset is a lock-skipping hint for normal insert/remove
    // paths, not a replacement for the pointer hash.  Iterate only advertised
    // active shards, but always include the pointer's home shard and fall back
    // to all shards if legacy/test state has a nonzero record count with a zero
    // mask.  This keeps stale masks from turning a valid home record into a
    // silent recovery miss.
    let mut active_offsets =
        auto_allocation_record_probe_offsets_including_home(active_mask, home_shard_idx);
    while let Some((shard_idx, shard_offset)) =
        pop_next_auto_allocation_record_active_shard(home_shard_idx, &mut active_offsets)
    {
        let start = if shard_offset == 0 { home_start } else { 0 };
        let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();

        if let Some(idx) = global_auto_allocation_record_hot_slot(&*table, ptr_key) {
            let record = table.inline[idx];
            if record.ptr == ptr_key {
                if record.matches_allocation(ptr, layout) {
                    if remove {
                        remove_global_auto_allocation_inline_record(
                            shard_idx,
                            &mut *table,
                            idx,
                            ptr_key,
                        );
                    }
                    return Some(record.metadata);
                }
                return None;
            }
            clear_global_auto_allocation_record_hot_slot(&mut *table, ptr_key);
        }

        let mut offset = 0;
        while offset < AUTO_ALLOCATION_RECORD_PROBE_LIMIT {
            let idx = (start + offset) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1);
            let record = table.inline[idx];
            if record.is_empty() {
                break;
            }
            if record.ptr == ptr_key {
                if record.matches_allocation(ptr, layout) {
                    if remove {
                        remove_global_auto_allocation_inline_record(
                            shard_idx,
                            &mut *table,
                            idx,
                            ptr_key,
                        );
                    } else {
                        remember_global_auto_allocation_record_hot_slot(&mut *table, ptr_key, idx);
                    }
                    return Some(record.metadata);
                }
                return None;
            }
            offset += 1;
        }

        #[cfg(not(feature = "fixed_heap"))]
        {
            // See `update_global_auto_allocation_record_in_shard`: overflow
            // records are only authoritative in the pointer's home shard.
            if shard_idx == home_shard_idx {
                if let Some(slot) = find_auto_allocation_record_in_overflow(table.overflow, ptr_key)
                {
                    let record = unsafe { *slot };
                    if record.matches_allocation(ptr, layout) {
                        if remove {
                            if atomic_saturating_decrement(&AUTO_ALLOCATION_RECORD_COUNT) == 0 {
                                unsafe {
                                    *slot = AutoAllocationRecord::empty();
                                }
                                table.live_count = table.live_count.saturating_sub(1);
                                AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.store(0, Ordering::Release);
                                semantic_slow_path_clear(SLOW_PATH_ALLOCATION_RECORDS);
                            } else {
                                unsafe {
                                    *slot = AutoAllocationRecord::tombstone();
                                }
                                decrement_global_auto_allocation_record_shard_live_count(
                                    shard_idx,
                                    &mut *table,
                                );
                            }
                            unsafe {
                                release_empty_auto_allocation_record_overflow_pages(
                                    &mut table.overflow,
                                );
                            }
                        }
                        return Some(record.metadata);
                    }
                    return None;
                }
            }
        }

        #[cfg(feature = "fixed_heap")]
        {
            let mut slow_offset = AUTO_ALLOCATION_RECORD_PROBE_LIMIT;
            while slow_offset < AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
                let idx = (start + slow_offset) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1);
                let record = table.inline[idx];
                if record.is_empty() {
                    break;
                }
                if record.ptr == ptr_key {
                    if record.matches_allocation(ptr, layout) {
                        if remove {
                            remove_global_auto_allocation_inline_record(
                                shard_idx,
                                &mut *table,
                                idx,
                                ptr_key,
                            );
                        } else {
                            remember_global_auto_allocation_record_hot_slot(
                                &mut *table,
                                ptr_key,
                                idx,
                            );
                        }
                        return Some(record.metadata);
                    }
                    return None;
                }
                slow_offset += 1;
            }
        }
    }
    None
}

fn lookup_auto_allocation_metadata(ptr: *mut u8, layout: Layout) -> Option<AllocationMetadata> {
    recover_auto_allocation_record_metadata(ptr, layout, false)
}

/// Return metadata recorded for this exact allocation pointer without falling
/// back to process-wide layout/auto metadata.
///
/// Scoped compiler-lowering paths use this when reallocating inside a different
/// active metadata scope: the old object must be released under the metadata it
/// was allocated with, while the new object uses the current active scope.
pub(crate) fn recorded_reallocation_old_metadata(
    ptr: *mut u8,
    layout: Layout,
) -> Option<AllocationMetadata> {
    lookup_auto_allocation_metadata(ptr, layout)
}

pub(crate) fn auto_reallocation_old_metadata(
    ptr: *mut u8,
    layout: Layout,
) -> Option<AllocationMetadata> {
    recorded_reallocation_old_metadata(ptr, layout).or_else(|| auto_deallocation_metadata(layout))
}

/// Recover compiler-stream auto metadata for an ordinary `GlobalAlloc`
/// deallocation without consuming the next allocation-site stream entry.
pub fn take_auto_deallocation_metadata(ptr: *mut u8, layout: Layout) -> Option<AllocationMetadata> {
    recover_auto_allocation_record_metadata(ptr, layout, true)
}

/// Return the semantic metadata currently associated with ordinary
/// `GlobalAlloc` calls on this thread.
///
/// Compiler instrumentation can use the scope-enter/scope-exit ABI below to
/// make otherwise unmodified standard-library allocation sites flow through the
/// semantic path. When no scope is active, normal `GlobalAlloc` calls remain
/// fallback evidence for coverage denominators.
pub fn active_allocation_metadata() -> Option<AllocationMetadata> {
    unsafe {
        let metadata = ACTIVE_METADATA;
        if metadata == AllocationMetadata::unknown() {
            None
        } else {
            Some(metadata)
        }
    }
}

/// Install active semantic metadata for this thread and return the previous
/// value so callers can restore it after an instrumented allocation scope.
pub unsafe fn set_active_metadata(metadata: AllocationMetadata) -> AllocationMetadata {
    replace_active_metadata(metadata)
}

/// Restore a previously saved semantic metadata scope.
pub unsafe fn restore_active_metadata(previous: AllocationMetadata) {
    let _ = replace_active_metadata(previous);
}

/// Run `f` while ordinary global-allocation calls on this thread inherit
/// `metadata`.
pub fn with_semantic_metadata<R>(metadata: AllocationMetadata, f: impl FnOnce() -> R) -> R {
    let _guard = MetadataScopeGuard {
        previous: unsafe { set_active_metadata(metadata) },
    };
    f()
}

/// Convenience helper for source-level or compiler-prototype experiments that
/// classify a region by Rust type.
pub fn with_rust_type_metadata_at<T, R>(
    module_id: u64,
    flags: u32,
    callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    let metadata = AllocationMetadata::for_rust_type::<T>()
        .with_module(module_id)
        .with_callsite(callsite)
        .with_flags(flags);
    with_semantic_metadata(metadata, f)
}

#[inline]
fn type_cache_avalanche(mut value: u64) -> u64 {
    value ^= value >> 33;
    value = value.wrapping_mul(0xff51_afd7_ed55_8ccd);
    value ^= value >> 33;
    value = value.wrapping_mul(0xc4ce_b9fe_1a85_ec53);
    value ^ (value >> 33)
}

#[inline]
fn type_cache_identity_metadata_matches(
    cached: AllocationMetadata,
    requested: AllocationMetadata,
) -> bool {
    cached.type_id == requested.type_id
        && cached.module_id == requested.module_id
        && cached.flags == requested.flags
        && cached.lifetime_hint == requested.lifetime_hint
        && cached.placement_hint == requested.placement_hint
}

#[inline]
fn compute_type_cache_identity_key(metadata: AllocationMetadata) -> u64 {
    // Cache reuse is intentionally allocation-site agnostic: a compiler Drop
    // site and a later allocation site for the same object class may have
    // different callsite hashes.  Module/context and policy/lifetime placement
    // are isolation boundaries and must not alias merely because type_id
    // collides or is reused by another crate/context.
    //
    // This is a per-process runtime cache key, not an on-disk/ABI-stable digest.
    // Keep the same field contract as the previous FNV path while avoiding byte
    // loops on the compiler-scoped alloc/free hot path.
    let mut key = 0x9e37_79b9_7f4a_7c15u64;
    key ^= metadata.type_id.wrapping_mul(0xbf58_476d_1ce4_e5b9);
    key = key.rotate_left(27);
    key ^= metadata.module_id.wrapping_mul(0x94d0_49bb_1331_11ebu64);
    key = key.rotate_left(31);
    key ^= (metadata.flags as u64).wrapping_mul(0xd6e8_feb8_6659_fd93u64);
    key = key.rotate_left(17);
    key ^= (((metadata.lifetime_hint as u64) << 32) | metadata.placement_hint as u64)
        .wrapping_mul(0xa076_1d64_78bd_642fu64);
    non_zero_hash(type_cache_avalanche(key))
}

#[inline]
fn type_cache_identity_key(metadata: AllocationMetadata) -> u64 {
    #[cfg(unialloc_target_arm64e)]
    {
        // Current arm64e Rust TLV descriptors fault in no_std probes.  The
        // identity hot slot is an optimization only, so compute the key
        // directly on that target and leave the ordinary TLS fast path intact
        // everywhere else.
        return compute_type_cache_identity_key(metadata);
    }
    #[cfg(not(unialloc_target_arm64e))]
    unsafe {
        let hot = TYPE_CACHE_IDENTITY_HOT_SLOT;
        if hot.active && type_cache_identity_metadata_matches(hot.metadata, metadata) {
            #[cfg(test)]
            TYPE_CACHE_IDENTITY_HOT_HITS.fetch_add(1, Ordering::Relaxed);
            return hot.cache_key;
        }

        let cache_key = compute_type_cache_identity_key(metadata);
        TYPE_CACHE_IDENTITY_HOT_SLOT = TypeCacheIdentityHotSlot {
            metadata,
            cache_key,
            active: true,
        };
        cache_key
    }
}

#[inline]
pub(crate) fn allocation_metadata_recovery_identity_matches(
    requested: AllocationMetadata,
    recorded: AllocationMetadata,
) -> bool {
    requested.has_type()
        && recorded.has_type()
        && !requested.is_layout_derived()
        && !recorded.is_layout_derived()
        && type_cache_identity_key(recorded) == type_cache_identity_key(requested)
}

#[inline]
pub(crate) fn deallocation_metadata_after_recovery_record(
    requested: AllocationMetadata,
    recorded: AllocationMetadata,
) -> AllocationMetadata {
    if allocation_metadata_recovery_identity_matches(requested, recorded) {
        SEMANTIC_METADATA_VALIDATION.record_recovery_identity_match();
        // Preserve caller-provided deallocation/drop-site callsite metadata when
        // the exact allocation record agrees on the allocator-visible identity.
        // The cache identity intentionally excludes callsite so allocation and
        // Drop sites for the same object class can still share reuse state.
        requested
    } else {
        if requested.has_type()
            && recorded.has_type()
            && !requested.is_layout_derived()
            && !recorded.is_layout_derived()
        {
            SEMANTIC_METADATA_VALIDATION.record_recovery_identity_mismatch(requested, recorded);
        }
        // A recovery record is recorded at allocation completion and is the
        // authoritative heap-object identity.  If an explicit compiler/runtime
        // deallocation ABI supplies stale type/module/policy fields, downstream
        // cache, guard, delayed-free, memory-tag, and stats decisions must use
        // the recorded allocation identity instead of poisoning the wrong class.
        recorded
    }
}

#[inline]
fn type_cache_slot(cache_key: u64) -> usize {
    (cache_key as usize).wrapping_mul(0x9E37_79B1usize) & (TYPE_CACHE_SLOTS - 1)
}

#[inline]
fn plain_type_cache_slot_key(identity_key: u64, layout: Layout) -> u64 {
    // `identity_key` is intentionally layout-agnostic so allocation and Drop
    // sites for the same heap-object class can agree on reuse identity.  The
    // cold linked-list cache, however, stores concrete object layouts in the
    // object header.  Mixing size/align into the *slot* key keeps different
    // layouts for the same type in separate probe windows without adding per
    // slot metadata or weakening the allocator-visible type identity.
    let mut key = identity_key ^ (layout.size() as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    key = key.rotate_left(29);
    key ^= (layout.align() as u64).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    non_zero_hash(type_cache_avalanche(key))
}

#[inline]
unsafe fn remember_type_cache_hot_slot(cache_key: u64, slot_idx: usize) {
    TYPE_CACHE_HOT_SLOT = TypeCacheHotSlot {
        cache_key,
        slot_idx,
    };
}

#[inline]
unsafe fn clear_type_cache_hot_slot(cache_key: u64) {
    if TYPE_CACHE_HOT_SLOT.cache_key == cache_key {
        TYPE_CACHE_HOT_SLOT = TypeCacheHotSlot::empty();
    }
}

#[inline]
unsafe fn clear_type_cache_slot(slot: &mut TypeCacheSlot, cache_key: u64) {
    slot.clear();
    clear_type_cache_hot_slot(cache_key);
}

unsafe fn type_cache_slot_head_is_valid(slot: &TypeCacheSlot) -> bool {
    if slot.count == 0 {
        return true;
    }
    if slot.head.is_null() || (slot.head as usize) & (TYPE_CACHE_NODE_ALIGN - 1) != 0 {
        return false;
    }

    let node = slot.head as *mut usize;
    let size = node.add(1).read();
    let align = node.add(2).read();
    Layout::from_size_align(size, align).is_ok()
}

unsafe fn type_cache_slot_retained_bytes(slot: &TypeCacheSlot) -> Option<usize> {
    if slot.has_corrupt_links() {
        return None;
    }

    let mut total = 0usize;
    let mut current = slot.head;
    let mut remaining = slot.count;
    while remaining > 0 {
        if current.is_null() || (current as usize) & (TYPE_CACHE_NODE_ALIGN - 1) != 0 {
            return None;
        }

        let node = current as *mut usize;
        let next = node.read() as *mut u8;
        let size = node.add(1).read();
        let align = node.add(2).read();
        if Layout::from_size_align(size, align).is_err() {
            return None;
        }

        total = total.saturating_add(type_cache_retained_bytes_for_object(size));
        current = next;
        remaining -= 1;
    }

    if !current.is_null() {
        return None;
    }

    Some(total)
}

unsafe fn repair_type_cache_slot_accounting_if_possible(slot: &mut TypeCacheSlot) -> bool {
    if slot.has_corrupt_links() {
        return false;
    }
    if !slot.has_corrupt_accounting() {
        return true;
    }

    // As with the metadata-segregated bucket table, the retained-byte counter is
    // budget accounting.  A valid linked list should be recovered by recomputing
    // the counter; clearing it would leak the still-owned cached objects.
    match type_cache_slot_retained_bytes(slot) {
        Some(total)
            if total <= MAX_TYPE_CACHE_SLOT_BYTES
                && ((slot.count == 0 && total == 0) || (slot.count != 0 && total != 0)) =>
        {
            slot.retained_bytes = total;
            true
        }
        _ => false,
    }
}

unsafe fn plain_type_cache_trusted_retained_bytes() -> Option<usize> {
    let mut retained_bytes = if INLINE_TYPE_CACHE_ENTRY.is_empty() {
        0
    } else {
        INLINE_TYPE_CACHE_ENTRY.retained_bytes()
    };

    let mut idx = 0usize;
    while idx < TYPE_CACHE_SLOTS {
        let slot = TYPE_CACHE[idx];
        let occupied = slot.count != 0 || !slot.head.is_null();
        if slot.is_corrupt() {
            if occupied {
                // A corrupt slot may still own cached objects, so do not treat
                // the aggregate budget as having spare capacity until the
                // normal probe/repair path has cleared or repaired that slot.
                return None;
            }
        } else if occupied {
            retained_bytes = retained_bytes.saturating_add(slot.retained_bytes);
        }
        idx += 1;
    }

    Some(retained_bytes)
}

#[inline]
unsafe fn plain_type_cache_can_accept_retained_bytes(incoming_retained_bytes: usize) -> bool {
    match plain_type_cache_trusted_retained_bytes() {
        Some(retained_bytes) => {
            retained_bytes.saturating_add(incoming_retained_bytes)
                <= MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES
        }
        None => false,
    }
}

#[inline]
unsafe fn type_cache_hot_slot(cache_key: u64) -> Option<*mut TypeCacheSlot> {
    if TYPE_CACHE_HOT_SLOT.cache_key == cache_key {
        let idx = TYPE_CACHE_HOT_SLOT.slot_idx & (TYPE_CACHE_SLOTS - 1);
        let slot = &mut TYPE_CACHE[idx];
        if slot.cache_key == cache_key && !repair_type_cache_slot_accounting_if_possible(slot) {
            clear_type_cache_slot(slot, cache_key);
            return None;
        }
        if slot.cache_key == cache_key {
            return Some(slot as *mut TypeCacheSlot);
        }
        clear_type_cache_hot_slot(cache_key);
    }
    None
}

unsafe fn type_cache_find_slot_for_key(
    cache_key: u64,
    allow_empty: bool,
) -> Option<*mut TypeCacheSlot> {
    if let Some(slot) = type_cache_hot_slot(cache_key) {
        return Some(slot);
    }

    let start = type_cache_slot(cache_key);
    let mut first_empty: *mut TypeCacheSlot = core::ptr::null_mut();
    let mut first_empty_idx = 0usize;
    let mut offset = 0;
    while offset < TYPE_CACHE_PROBE_LIMIT {
        let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
        let slot = &mut TYPE_CACHE[idx];
        if !repair_type_cache_slot_accounting_if_possible(slot) {
            if slot.cache_key == cache_key {
                clear_type_cache_slot(slot, cache_key);
            } else {
                slot.clear();
            }
        }
        if slot.cache_key == cache_key {
            remember_type_cache_hot_slot(cache_key, idx);
            return Some(slot as *mut TypeCacheSlot);
        }
        if allow_empty && first_empty.is_null() && slot.cache_key == UNKNOWN_SEMANTIC_ID {
            first_empty = slot as *mut TypeCacheSlot;
            first_empty_idx = idx;
        }
        offset += 1;
    }
    if allow_empty && !first_empty.is_null() {
        remember_type_cache_hot_slot(cache_key, first_empty_idx);
        Some(first_empty)
    } else {
        None
    }
}

#[inline]
fn segregated_type_cache_slot(metadata: AllocationMetadata, layout: Layout) -> usize {
    segregated_type_cache_slot_for_key(type_cache_identity_key(metadata), layout)
}

#[inline]
fn segregated_type_cache_slot_for_key(cache_key: u64, layout: Layout) -> usize {
    segregated_type_cache_slot_for_parts(cache_key, layout.size(), layout.align())
}

#[inline]
fn segregated_type_cache_slot_for_parts(cache_key: u64, size: usize, align: usize) -> usize {
    let mixed = cache_key ^ (size as u64).rotate_left(17) ^ (align as u64).rotate_left(31);
    type_cache_slot(mixed)
}

#[inline]
fn segregated_type_cache_policy_key(metadata: AllocationMetadata) -> u32 {
    metadata.flags & SEGREGATED_TYPE_CACHE_POLICY_MASK
}

#[inline]
unsafe fn remember_segregated_type_cache_hot_bucket(
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
    cache_domain: u8,
    bucket_idx: usize,
) {
    SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket {
        cache_key,
        size: layout.size(),
        align: layout.align(),
        policy_key,
        cache_domain,
        bucket_idx,
    };
}

#[inline]
unsafe fn clear_segregated_type_cache_hot_bucket(
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
    cache_domain: u8,
) {
    if SEGREGATED_TYPE_CACHE_HOT_BUCKET.matches_cached_key(
        layout,
        cache_key,
        policy_key,
        cache_domain,
    ) {
        SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket::empty();
    }
}

#[inline]
unsafe fn segregated_type_cache_hot_bucket(
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
    cache_domain: u8,
) -> Option<usize> {
    if SEGREGATED_TYPE_CACHE_HOT_BUCKET.matches_cached_key(
        layout,
        cache_key,
        policy_key,
        cache_domain,
    ) {
        Some(SEGREGATED_TYPE_CACHE_HOT_BUCKET.bucket_idx & (TYPE_CACHE_SLOTS - 1))
    } else {
        None
    }
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
fn hugepage_metadata_mapping_size(payload_size: usize) -> Option<usize> {
    payload_size
        .checked_add(HUGEPAGE_METADATA_MAPPING_GRANULARITY - 1)
        .map(|size| size & !(HUGEPAGE_METADATA_MAPPING_GRANULARITY - 1))
}

#[cfg(not(feature = "fixed_heap"))]
#[inline]
fn compact_metadata_mapping_size(payload_size: usize) -> Option<usize> {
    payload_size
        .checked_add(crate::PAGE_SIZE - 1)
        .map(|size| size & !(crate::PAGE_SIZE - 1))
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn ensure_hugepage_segregated_type_cache(
) -> Option<&'static mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]> {
    if HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
        let size = size_of::<[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]>();
        let hugepage_mapping_size = hugepage_metadata_mapping_size(size)?;
        let prot = system_alloc::prots::get_prot(true, true, false);
        let mapping = system_alloc::mmap_huge_with_backing(hugepage_mapping_size, prot);
        let mut ptr = mapping.ptr;
        if ptr.is_null() {
            HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING = mapping.backing;
            return None;
        }
        let mut mapping_size = hugepage_mapping_size;
        let mut backing = mapping.backing;

        // Preserve the hugepage path when the platform provides it.  If the
        // request falls back to ordinary pages, do not retain a hugepage-sized
        // mapping for a small metadata table: remap the side-cache at ordinary
        // page granularity.  The original hugepage attempt/fallback remains
        // visible in global mmap stats, while this TLS cache records the actual
        // compact backing it retains.  If the compact remap fails, keep the
        // original fallback mapping so allocator behavior does not regress.
        if backing.is_fallback() {
            if let Some(compact_size) = compact_metadata_mapping_size(size) {
                if compact_size < hugepage_mapping_size {
                    let compact =
                        system_alloc::normalize_mmap_result(system_alloc::mmap(compact_size, prot));
                    if !compact.is_null() {
                        system_alloc::munmap(ptr, hugepage_mapping_size);
                        ptr = compact;
                        mapping_size = compact_size;
                        backing = system_alloc::HugePageMmapBacking::OrdinaryFallback;
                    }
                }
            }
        }

        let ptr = ptr as *mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS];
        ptr.write([SegregatedTypeCacheBucket::empty(); TYPE_CACHE_SLOTS]);
        HUGEPAGE_SEGREGATED_TYPE_CACHE = ptr;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE = mapping_size;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING = backing;
    }
    Some(&mut *HUGEPAGE_SEGREGATED_TYPE_CACHE)
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn hugepage_segregated_type_cache_if_allocated(
) -> Option<&'static mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]> {
    if HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
        None
    } else {
        Some(&mut *HUGEPAGE_SEGREGATED_TYPE_CACHE)
    }
}

#[cfg(not(feature = "fixed_heap"))]
fn segregated_type_cache_buckets_are_empty(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
) -> bool {
    cache
        .iter()
        .all(|bucket| !bucket.is_corrupt() && bucket.count == 0)
}

#[cfg(not(feature = "fixed_heap"))]
fn observed_hugepage_segregated_occupied_buckets(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
) -> usize {
    let mut occupied = 0usize;
    for bucket in cache.iter() {
        // `count` is the compact-prefix length for healthy buckets.  A corrupt
        // bucket is also treated as occupied here: release must not unmap the
        // side-cache while any visible/corrupt state could still represent an
        // owned cached object.  Normal pop/push corruption recovery remains the
        // place that clears such buckets deliberately.
        if bucket.count != 0 || bucket.is_corrupt() {
            occupied = occupied.saturating_add(1);
        }
    }
    occupied
}

fn segregated_type_cache_footprint_snapshot(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
) -> SegregatedTypeCacheFootprintSnapshot {
    let mut occupied_buckets = 0usize;
    let mut occupied_entries = 0usize;
    let mut retained_bytes = 0usize;
    let mut corrupt_buckets = 0usize;

    for bucket in cache.iter() {
        let count = bucket.count;
        if bucket.is_corrupt() {
            corrupt_buckets = corrupt_buckets.saturating_add(1);
            let mut observed_entries = 0usize;
            for entry in bucket.entries.iter() {
                if !entry.is_empty() {
                    observed_entries = observed_entries.saturating_add(1);
                }
            }
            if count != 0 || observed_entries != 0 {
                occupied_buckets = occupied_buckets.saturating_add(1);
                occupied_entries = occupied_entries.saturating_add(observed_entries);
            }
        } else if count > 0 {
            occupied_buckets = occupied_buckets.saturating_add(1);
            occupied_entries = occupied_entries.saturating_add(count);
            retained_bytes = retained_bytes.saturating_add(bucket.retained_bytes);
        }
    }

    SegregatedTypeCacheFootprintSnapshot {
        occupied_buckets,
        occupied_entries,
        retained_bytes,
        corrupt_buckets,
    }
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn release_empty_hugepage_segregated_type_cache() {
    if HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
        return;
    }

    let observed_occupied =
        observed_hugepage_segregated_occupied_buckets(&*HUGEPAGE_SEGREGATED_TYPE_CACHE);
    HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = observed_occupied;
    if observed_occupied != 0 {
        return;
    }
    #[cfg(test)]
    debug_assert!(segregated_type_cache_buckets_are_empty(
        &*HUGEPAGE_SEGREGATED_TYPE_CACHE
    ));
    let mapping_size = if HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE == 0 {
        size_of::<[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]>()
    } else {
        HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE
    };
    if SEGREGATED_TYPE_CACHE_HOT_BUCKET.cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE {
        SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket::empty();
    }
    system_alloc::munmap(HUGEPAGE_SEGREGATED_TYPE_CACHE as *mut u8, mapping_size);
    HUGEPAGE_SEGREGATED_TYPE_CACHE = core::ptr::null_mut();
    HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE = 0;
    HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
    HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
    HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
    HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING = system_alloc::HugePageMmapBacking::Unmapped;
}

#[cfg(not(feature = "fixed_heap"))]
pub fn hugepage_metadata_side_cache_backing_snapshot() -> Option<system_alloc::HugePageMmapBacking>
{
    hugepage_metadata_side_cache_snapshot().map(|snapshot| snapshot.backing)
}

#[cfg(not(feature = "fixed_heap"))]
pub fn hugepage_metadata_side_cache_snapshot() -> Option<HugepageMetadataSideCacheSnapshot> {
    unsafe {
        if HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
            None
        } else {
            let footprint =
                segregated_type_cache_footprint_snapshot(&*HUGEPAGE_SEGREGATED_TYPE_CACHE);
            let inline_entries = inline_segregated_type_cache_occupied_entry_count(
                SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
            );
            let inline_retained_bytes =
                inline_segregated_type_cache_retained_bytes(SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE);
            Some(HugepageMetadataSideCacheSnapshot {
                allocated: true,
                address: HUGEPAGE_SEGREGATED_TYPE_CACHE as usize,
                mapping_size: HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE,
                backing: HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING,
                occupied_buckets: footprint.occupied_buckets,
                occupied_entries: footprint.occupied_entries.saturating_add(inline_entries),
                retained_bytes: footprint
                    .retained_bytes
                    .saturating_add(inline_retained_bytes),
                corrupt_buckets: footprint.corrupt_buckets,
            })
        }
    }
}

pub fn metadata_segregation_side_cache_snapshot() -> MetadataSegregationSideCacheSnapshot {
    unsafe {
        let inline_entries = inline_segregated_type_cache_occupied_entry_count(
            SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
        );
        let inline_occupied = inline_entries != 0;
        let inline_retained_bytes =
            inline_segregated_type_cache_retained_bytes(SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY);
        let cache = &*core::ptr::addr_of!(SEGREGATED_TYPE_CACHE);
        let footprint = segregated_type_cache_footprint_snapshot(cache);
        MetadataSegregationSideCacheSnapshot {
            inline_occupied,
            occupied_buckets: footprint.occupied_buckets,
            occupied_entries: footprint.occupied_entries.saturating_add(inline_entries),
            retained_bytes: footprint
                .retained_bytes
                .saturating_add(inline_retained_bytes),
            corrupt_buckets: footprint.corrupt_buckets,
            hot_bucket_active: SEGREGATED_TYPE_CACHE_HOT_BUCKET.cache_key != UNKNOWN_SEMANTIC_ID,
        }
    }
}

fn segregated_type_cache_bucket_has_visible_state(bucket: SegregatedTypeCacheBucket) -> bool {
    if bucket.count != 0 {
        return true;
    }

    let mut idx = 0usize;
    while idx < SEGREGATED_TYPE_CACHE_DEPTH {
        if !bucket.entries[idx].is_empty() {
            return true;
        }
        idx += 1;
    }
    false
}

#[inline]
unsafe fn inline_segregated_type_cache_retained_bytes(cache_domain: u8) -> usize {
    let mut retained_bytes = 0usize;
    let mut index = 0usize;
    while index < inline_segregated_type_cache_capacity(cache_domain) {
        let entry = *inline_segregated_type_cache_entry_at(cache_domain, index);
        if !entry.is_empty() {
            retained_bytes = retained_bytes.saturating_add(entry.retained_bytes());
        }
        index += 1;
    }
    retained_bytes
}

#[inline]
unsafe fn inline_segregated_type_cache_occupied_entry_count(cache_domain: u8) -> usize {
    let mut occupied = 0usize;
    let mut index = 0usize;
    while index < inline_segregated_type_cache_capacity(cache_domain) {
        if !(*inline_segregated_type_cache_entry_at(cache_domain, index)).is_empty() {
            occupied = occupied.saturating_add(1);
        }
        index += 1;
    }
    occupied
}

unsafe fn segregated_type_cache_trusted_retained_bytes(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
) -> Option<usize> {
    let inline_retained_bytes = inline_segregated_type_cache_retained_bytes(cache_domain);
    if segregated_type_cache_bucket_retained_bytes_trusted(cache_domain) {
        return Some(
            segregated_type_cache_bucket_retained_bytes(cache_domain)
                .saturating_add(inline_retained_bytes),
        );
    }

    record_segregated_type_cache_aggregate_scan();
    let mut bucket_retained_bytes = 0usize;

    let mut idx = 0usize;
    while idx < TYPE_CACHE_SLOTS {
        let bucket = cache[idx];
        if bucket.is_corrupt() {
            if segregated_type_cache_bucket_has_visible_state(bucket) {
                // Corrupt bucket entries may still own cached objects.  Refuse
                // new side-cache growth until normal bucket probing repairs or
                // clears the visible state.
                return None;
            }
        } else if bucket.count != 0 {
            bucket_retained_bytes = bucket_retained_bytes.saturating_add(bucket.retained_bytes);
        }
        idx += 1;
    }

    set_segregated_type_cache_bucket_retained_bytes(cache_domain, bucket_retained_bytes, true);
    Some(bucket_retained_bytes.saturating_add(inline_retained_bytes))
}

fn segregated_type_cache_projected_aggregate_retained_bytes(
    current_retained_bytes: usize,
    bucket: SegregatedTypeCacheBucket,
    incoming_size: usize,
) -> Option<usize> {
    if bucket.is_corrupt() {
        return None;
    }

    let incoming_retained_bytes = type_cache_retained_bytes_for_object(incoming_size);
    let evicted_retained_bytes = if bucket.count >= SEGREGATED_TYPE_CACHE_DEPTH {
        let evict_idx = bucket.evict_cursor & (SEGREGATED_TYPE_CACHE_DEPTH - 1);
        bucket.entries[evict_idx].retained_bytes()
    } else {
        0
    };

    Some(
        current_retained_bytes
            .saturating_sub(evicted_retained_bytes)
            .saturating_add(incoming_retained_bytes),
    )
}

unsafe fn segregated_type_cache_can_accept_aggregate(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
    bucket_idx: usize,
    incoming_size: usize,
) -> bool {
    let current_retained_bytes =
        match segregated_type_cache_trusted_retained_bytes(cache, cache_domain) {
            Some(retained_bytes) => retained_bytes,
            None => return false,
        };
    match segregated_type_cache_projected_aggregate_retained_bytes(
        current_retained_bytes,
        cache[bucket_idx],
        incoming_size,
    ) {
        Some(projected) => projected <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES,
        None => false,
    }
}

#[inline]
unsafe fn segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
    inline_cache_domain: u8,
    cached_retained_bytes: &mut Option<usize>,
    bucket_idx: usize,
    incoming_size: usize,
) -> bool {
    let current_retained_bytes = match *cached_retained_bytes {
        Some(retained_bytes) => retained_bytes,
        None => match segregated_type_cache_trusted_retained_bytes_for_inline_domain(
            cache,
            cache_domain,
            inline_cache_domain,
        ) {
            Some(retained_bytes) => {
                *cached_retained_bytes = Some(retained_bytes);
                retained_bytes
            }
            None => return false,
        },
    };
    match segregated_type_cache_projected_aggregate_retained_bytes(
        current_retained_bytes,
        cache[bucket_idx],
        incoming_size,
    ) {
        Some(projected) => projected <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES,
        None => false,
    }
}

#[inline]
unsafe fn segregated_type_cache_trusted_retained_bytes_for_inline_domain(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
    inline_cache_domain: u8,
) -> Option<usize> {
    segregated_type_cache_trusted_retained_bytes(cache, cache_domain).map(|mut retained_bytes| {
        if inline_cache_domain != cache_domain {
            retained_bytes = retained_bytes.saturating_add(
                inline_segregated_type_cache_retained_bytes(inline_cache_domain),
            );
        }
        retained_bytes
    })
}

#[inline]
unsafe fn segregated_type_cache_can_grow_inline_aggregate(
    cache: &[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
    inline_cache_domain: u8,
    incoming_size: usize,
) -> bool {
    match segregated_type_cache_trusted_retained_bytes_for_inline_domain(
        cache,
        cache_domain,
        inline_cache_domain,
    ) {
        Some(retained_bytes) => {
            retained_bytes.saturating_add(type_cache_retained_bytes_for_object(incoming_size))
                <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
        }
        None => false,
    }
}

pub fn type_isolation_side_cache_snapshot() -> TypeIsolationSideCacheSnapshot {
    unsafe {
        let mut occupied_slots = 0usize;
        let mut occupied_entries: usize = if INLINE_TYPE_CACHE_ENTRY.is_empty() {
            0
        } else {
            1
        };
        let mut retained_bytes = if INLINE_TYPE_CACHE_ENTRY.is_empty() {
            0
        } else {
            INLINE_TYPE_CACHE_ENTRY.retained_bytes()
        };
        let mut corrupt_slots = 0usize;
        let mut idx = 0usize;
        while idx < TYPE_CACHE_SLOTS {
            let slot = TYPE_CACHE[idx];
            let occupied = slot.count != 0 || !slot.head.is_null();
            if slot.is_corrupt() {
                corrupt_slots = corrupt_slots.saturating_add(1);
                if occupied {
                    occupied_slots = occupied_slots.saturating_add(1);
                    occupied_entries = occupied_entries
                        .saturating_add(core::cmp::min(slot.count, MAX_TYPE_CACHE_DEPTH));
                }
            } else if occupied {
                occupied_slots = occupied_slots.saturating_add(1);
                occupied_entries = occupied_entries
                    .saturating_add(core::cmp::min(slot.count, MAX_TYPE_CACHE_DEPTH));
                retained_bytes = retained_bytes.saturating_add(slot.retained_bytes);
            }
            idx += 1;
        }
        TypeIsolationSideCacheSnapshot {
            inline_occupied: !INLINE_TYPE_CACHE_ENTRY.is_empty(),
            occupied_slots,
            occupied_entries,
            retained_bytes,
            corrupt_slots,
            hot_slot_active: TYPE_CACHE_HOT_SLOT.cache_key != UNKNOWN_SEMANTIC_ID,
        }
    }
}

pub fn delayed_free_snapshot() -> DelayedFreeSnapshot {
    unsafe {
        let mut occupied_slots = 0usize;
        let mut retained_bytes = 0usize;
        let mut idx = 0usize;
        while idx < DELAYED_FREE_SLOTS {
            let slot = DELAYED_FREE[idx];
            if !slot.is_empty() {
                occupied_slots = occupied_slots.saturating_add(1);
                retained_bytes = retained_bytes.saturating_add(slot.retained_bytes());
            }
            idx += 1;
        }
        DelayedFreeSnapshot {
            occupied_slots,
            retained_bytes,
            tracked_retained_bytes: DELAYED_FREE_RETAINED_BYTES,
            retained_accounting_matches: retained_bytes == DELAYED_FREE_RETAINED_BYTES,
            cursor: DELAYED_FREE_CURSOR & (DELAYED_FREE_SLOTS - 1),
        }
    }
}

unsafe fn segregated_type_cache_for_pop(
    metadata: AllocationMetadata,
) -> (
    &'static mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    u8,
) {
    #[cfg(feature = "fixed_heap")]
    let _ = metadata;

    #[cfg(not(feature = "fixed_heap"))]
    {
        if metadata.requests(FLAG_HUGEPAGE_METADATA) {
            if let Some(cache) = hugepage_segregated_type_cache_if_allocated() {
                return (cache, SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE);
            }
        }
    }
    (
        ordinary_segregated_type_cache_mut(),
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
    )
}

unsafe fn segregated_type_cache_for_push(
    metadata: AllocationMetadata,
) -> (
    &'static mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    u8,
) {
    #[cfg(feature = "fixed_heap")]
    let _ = metadata;

    #[cfg(not(feature = "fixed_heap"))]
    {
        if metadata.requests(FLAG_HUGEPAGE_METADATA) {
            if let Some(cache) = ensure_hugepage_segregated_type_cache() {
                return (cache, SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE);
            }
        }
    }
    (
        ordinary_segregated_type_cache_mut(),
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
    )
}

#[inline]
fn metadata_integrity_required(metadata: AllocationMetadata) -> bool {
    metadata.requests(FLAG_POINTER_AUTH) || metadata.requests(FLAG_METADATA_PROTECTION)
}

#[inline]
fn metadata_side_table_required(metadata: AllocationMetadata) -> bool {
    metadata.requests(FLAG_METADATA_SEGREGATED)
        || metadata.requests(FLAG_HUGEPAGE_METADATA)
        || metadata_integrity_required(metadata)
}

#[inline]
fn semantic_stats_feature_enabled() -> bool {
    cfg!(feature = "stats")
}

#[inline]
fn semantic_stats_recording_flags() -> usize {
    if semantic_stats_feature_enabled() {
        let flags = SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed);
        #[cfg(test)]
        {
            // A few unit tests assert exact per-operation statistics while the
            // Rust test harness is still free to run unrelated allocator tests
            // in parallel. Production/evaluation statistics are process-wide;
            // this test-only owner gate lets those exact windows observe only
            // the thread that opened them, without changing the public ABI.
            if flags & (SLOW_PATH_STATS | SLOW_PATH_TYPE_STATS) != 0
                && !semantic_stats_test_exact_recording_allows_current_thread()
            {
                return flags & !(SLOW_PATH_STATS | SLOW_PATH_TYPE_STATS);
            }
        }
        flags
    } else {
        0
    }
}

#[inline]
fn semantic_stats_any_recording_enabled() -> bool {
    semantic_stats_recording_flags() & (SLOW_PATH_STATS | SLOW_PATH_TYPE_STATS) != 0
}

#[inline]
fn layout_derived_raw_only_fast_path(metadata: AllocationMetadata) -> bool {
    const RAW_ONLY_FLAGS: u32 = FLAG_TYPE_ISOLATED
        | FLAG_METADATA_SEGREGATED
        | FLAG_HUGEPAGE_METADATA
        | FLAG_POINTER_AUTH
        | FLAG_METADATA_PROTECTION;

    metadata.is_layout_derived()
        && (metadata.flags & !RAW_ONLY_FLAGS) == 0
        && !semantic_stats_any_recording_enabled()
}

const COMPILER_TYPE_METADATA_FAST_PATH_FLAGS: u32 = FLAG_TYPE_ISOLATED
    | FLAG_METADATA_SEGREGATED
    | FLAG_HUGEPAGE_METADATA
    | FLAG_POINTER_AUTH
    | FLAG_METADATA_PROTECTION;

#[inline]
fn compiler_type_isolated_recovery_fast_path(metadata: AllocationMetadata) -> bool {
    // This fast path is for compiler-supplied metadata whose policy side effects
    // are entirely cache/recovery-table local.  It intentionally excludes
    // policies that must run additional allocator hooks (zeroing, delayed free,
    // guard pages, or memory tags), but it can safely serve
    // metadata-segregated/hugepage/PAC/protected cache entries with the same
    // recovery-record semantics as the plain type-isolated path. Stats are
    // event accounting only; the fast alloc/dealloc helpers record those events
    // directly instead of forcing compiler-exact metadata through the slow path.
    metadata.has_type()
        && metadata.requests(FLAG_TYPE_ISOLATED)
        && (metadata.flags & !COMPILER_TYPE_METADATA_FAST_PATH_FLAGS) == 0
        && !metadata.is_layout_derived()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SemanticTypeCacheClass {
    Plain,
    Segregated,
}

#[inline]
fn classify_typed_object_cache_class(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<SemanticTypeCacheClass> {
    if layout.size() == 0 || layout.size() > MAX_TYPE_CACHE_OBJECT_SIZE {
        return None;
    }

    if metadata_side_table_required(metadata) {
        Some(SemanticTypeCacheClass::Segregated)
    } else if layout.size() >= MIN_TYPE_CACHE_OBJECT_SIZE {
        Some(SemanticTypeCacheClass::Plain)
    } else {
        None
    }
}

#[inline]
fn semantic_type_cache_class(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<SemanticTypeCacheClass> {
    if !metadata.has_type()
        || !metadata.requests(FLAG_TYPE_ISOLATED)
        || metadata.is_layout_derived()
    {
        return None;
    }

    classify_typed_object_cache_class(layout, metadata)
}

#[inline]
fn type_cache_eligible(layout: Layout, metadata: AllocationMetadata) -> bool {
    semantic_type_cache_class(layout, metadata) == Some(SemanticTypeCacheClass::Plain)
}

#[inline]
fn segregated_type_cache_eligible(layout: Layout, metadata: AllocationMetadata) -> bool {
    semantic_type_cache_class(layout, metadata) == Some(SemanticTypeCacheClass::Segregated)
}

#[inline]
fn compiler_type_metadata_cache_class(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<SemanticTypeCacheClass> {
    debug_assert!(compiler_type_isolated_recovery_fast_path(metadata));

    // The compiler metadata ABI already proved the type/isolation/layout-derived
    // preconditions in `compiler_type_isolated_recovery_fast_path`.  Reuse the
    // same size/policy classifier as the generic semantic path so this remains
    // a shortcut, not a second cache policy that can drift.
    classify_typed_object_cache_class(layout, metadata)
}

#[inline]
fn inline_segregated_type_cache_eligible_for_classified(metadata: AllocationMetadata) -> bool {
    !metadata.requests(FLAG_HUGEPAGE_METADATA)
        && (
            // The inline slot is the common one-object reuse path for any
            // ordinary out-of-line metadata policy.  PAC / metadata-protection
            // entries still carry and verify their authenticator through the
            // same SegregatedTypeCacheEntry, so forcing them through the bucket
            // table only adds probe overhead without improving isolation.
            metadata.requests(FLAG_METADATA_SEGREGATED) || metadata_integrity_required(metadata)
        )
}

#[inline]
fn inline_segregated_type_cache_pop_eligible(metadata: AllocationMetadata) -> bool {
    // Hugepage metadata may occupy either bounded inline entry before a third
    // cached object justifies allocating the mmap_huge-backed bucket table.
    inline_segregated_type_cache_eligible_for_classified(metadata)
        || metadata.requests(FLAG_HUGEPAGE_METADATA)
}

#[inline]
unsafe fn pop_inline_type_cache_eligible_with_key(
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
) -> Option<*mut u8> {
    let entry = INLINE_TYPE_CACHE_ENTRY;
    if entry.matches_cached_key(layout, cache_key) {
        INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        record_stats_type_cache_hit(metadata);
        Some(entry.ptr)
    } else {
        None
    }
}

#[inline]
unsafe fn push_inline_type_cache_eligible_with_key(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
) -> bool {
    if INLINE_TYPE_CACHE_ENTRY.is_empty() {
        INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry {
            cache_key,
            type_id: metadata.type_id,
            ptr,
            size: layout.size(),
            align: layout.align(),
        };
        record_stats_type_cache_insert(metadata);
        true
    } else {
        false
    }
}

unsafe fn pop_type_cache(layout: Layout, metadata: AllocationMetadata) -> Option<*mut u8> {
    let cache_key = type_cache_identity_key(metadata);
    pop_type_cache_with_key(layout, metadata, cache_key)
}

unsafe fn pop_type_cache_with_key(
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
) -> Option<*mut u8> {
    if !type_cache_eligible(layout, metadata) {
        record_stats_type_cache_bypass(metadata);
        return None;
    }
    pop_type_cache_eligible_with_key(layout, metadata, cache_key)
}

unsafe fn pop_type_cache_eligible_with_key(
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
) -> Option<*mut u8> {
    let slot_key = plain_type_cache_slot_key(cache_key, layout);
    let slot = match type_cache_find_slot_for_key(slot_key, false) {
        Some(slot) => &mut *slot,
        None => {
            record_stats_type_cache_bypass(metadata);
            return None;
        }
    };
    if slot.head.is_null() {
        clear_type_cache_slot(slot, slot_key);
        record_stats_type_cache_bypass(metadata);
        return None;
    }

    let mut current = slot.head;
    let mut previous: *mut usize = core::ptr::null_mut();
    let mut remaining = slot.count;
    while !current.is_null() && remaining > 0 {
        if (current as usize) & (TYPE_CACHE_NODE_ALIGN - 1) != 0 {
            clear_type_cache_slot(slot, slot_key);
            record_stats_type_cache_bypass(metadata);
            return None;
        }
        let node = current as *mut usize;
        let next = node.read() as *mut u8;
        let size = node.add(1).read();
        let align = node.add(2).read();
        if size == layout.size()
            && align == layout.align()
            && (current as usize) & (layout.align() - 1) == 0
        {
            let expected_tail_after_current = remaining.saturating_sub(1);
            if previous.is_null() {
                slot.head = next;
            } else {
                previous.write(next as usize);
            }
            slot.count -= 1;
            slot.retained_bytes = slot
                .retained_bytes
                .saturating_sub(type_cache_retained_bytes_for_object(size));
            if slot.count == 0
                || (expected_tail_after_current != 0 && next.is_null())
                || slot.has_corrupt_accounting()
            {
                // Emptying a slot must also clear the head pointer.  A corrupted
                // under- or over-counted chain should be dropped immediately
                // instead of leaving an UNKNOWN-key slot with a stale head that a
                // later push could splice back into the hot path.
                clear_type_cache_slot(slot, slot_key);
            }
            record_stats_type_cache_hit(metadata);
            return Some(current);
        }
        previous = node;
        current = next;
        remaining -= 1;
    }

    if !current.is_null() || remaining != 0 {
        clear_type_cache_slot(slot, slot_key);
    }
    record_stats_type_cache_bypass(metadata);
    None
}

unsafe fn push_type_cache(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) -> bool {
    let cache_key = type_cache_identity_key(metadata);
    push_type_cache_with_key(ptr, layout, metadata, cache_key)
}

unsafe fn push_type_cache_with_key(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
) -> bool {
    if ptr.is_null() || !type_cache_eligible(layout, metadata) {
        record_stats_type_cache_bypass(metadata);
        return false;
    }
    push_type_cache_eligible_with_key(ptr, layout, metadata, cache_key)
}

unsafe fn push_type_cache_eligible_with_key(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
) -> bool {
    if (ptr as usize) & (TYPE_CACHE_NODE_ALIGN - 1) != 0 {
        record_stats_type_cache_bypass(metadata);
        return false;
    }

    let slot_key = plain_type_cache_slot_key(cache_key, layout);
    let slot_ptr = match type_cache_find_slot_for_key(slot_key, true) {
        Some(slot) => slot,
        None => {
            record_stats_type_cache_bypass(metadata);
            return false;
        }
    };
    let retained_size = type_cache_retained_bytes_for_layout(layout);
    {
        let slot = &mut *slot_ptr;
        if slot.count >= MAX_TYPE_CACHE_DEPTH {
            record_stats_type_cache_bypass(metadata);
            return false;
        }
        if !type_cache_slot_head_is_valid(slot) {
            clear_type_cache_slot(slot, slot_key);
            record_stats_type_cache_bypass(metadata);
            return false;
        }
        debug_assert_eq!(
            type_cache_slot_retained_bytes(slot),
            Some(slot.retained_bytes),
            "plain type-cache retained-byte counter must match the linked nodes"
        );
        if slot.retained_bytes.saturating_add(retained_size) > MAX_TYPE_CACHE_SLOT_BYTES {
            record_stats_type_cache_bypass(metadata);
            return false;
        }
    }
    if !plain_type_cache_can_accept_retained_bytes(retained_size) {
        record_stats_type_cache_bypass(metadata);
        return false;
    }

    let slot = &mut *slot_ptr;
    let node = ptr as *mut usize;
    node.write(slot.head as usize);
    node.add(1).write(layout.size());
    node.add(2).write(layout.align());
    if slot.cache_key == UNKNOWN_SEMANTIC_ID {
        slot.cache_key = slot_key;
        slot.type_id = metadata.type_id;
    }
    slot.head = ptr;
    slot.count += 1;
    slot.retained_bytes = slot.retained_bytes.saturating_add(retained_size);
    record_stats_type_cache_insert(metadata);
    true
}

unsafe fn pop_segregated_type_cache_from(
    cache: &mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
    cache_domain: u8,
) -> Option<SegregatedTypeCacheEntry> {
    let start = segregated_type_cache_slot_for_key(cache_key, layout);
    let mut checked_hot_idx = None;
    if let Some(idx) = segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain)
    {
        checked_hot_idx = Some(idx);
        record_segregated_type_cache_bucket_probe_step();
        let bucket = &mut cache[idx];
        if clear_segregated_type_cache_bucket_if_corrupt(bucket, cache_domain) {
            clear_segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain);
        } else if let Some(entry) =
            pop_segregated_type_cache_bucket(bucket, cache_domain, layout, cache_key, policy_key)
        {
            return Some(entry);
        }
    }

    let mut offset = 0;
    while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
        let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
        if segregated_type_cache_probe_already_checked(checked_hot_idx, idx) {
            offset += 1;
            continue;
        }
        record_segregated_type_cache_bucket_probe_step();
        let bucket = &mut cache[idx];
        if clear_segregated_type_cache_bucket_if_corrupt(bucket, cache_domain) {
            clear_segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain);
        } else if let Some(entry) =
            pop_segregated_type_cache_bucket(bucket, cache_domain, layout, cache_key, policy_key)
        {
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                cache_domain,
                idx,
            );
            return Some(entry);
        }
        offset += 1;
    }

    None
}

unsafe fn finish_popped_segregated_type_cache_entry(
    layout: Layout,
    metadata: AllocationMetadata,
    entry: SegregatedTypeCacheEntry,
) -> *mut u8 {
    verify_metadata_record_auth(entry.ptr, layout, metadata, entry.metadata, entry.auth);
    record_stats_type_cache_hit(metadata);
    entry.ptr
}

#[cfg(unialloc_target_arm64e)]
#[inline]
unsafe fn arm64e_pop_inline_segregated_type_cache_entry(
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
) -> Option<SegregatedTypeCacheEntry> {
    let mut slot = ARM64E_INLINE_SEGREGATED_TYPE_CACHE_ENTRY.lock();
    let entry = *slot;
    if entry.matches_cached_key(layout, cache_key, policy_key) {
        *slot = SegregatedTypeCacheEntry::empty();
        Some(entry)
    } else {
        None
    }
}

#[cfg(unialloc_target_arm64e)]
#[inline]
unsafe fn arm64e_push_inline_segregated_type_cache_entry(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
    policy_key: u32,
) -> bool {
    let mut slot = ARM64E_INLINE_SEGREGATED_TYPE_CACHE_ENTRY.lock();
    if !slot.is_empty() {
        return false;
    }
    *slot = SegregatedTypeCacheEntry {
        cache_key,
        type_id: metadata.type_id,
        policy_key,
        ptr,
        size: layout.size(),
        align: layout.align(),
        auth: metadata_record_auth(ptr, layout, metadata),
        metadata,
    };
    record_stats_type_cache_insert(metadata);
    true
}

#[inline]
unsafe fn pop_inline_segregated_type_cache_eligible_with_key(
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
    inline_cache_domain: u8,
) -> Option<SegregatedTypeCacheEntry> {
    let mut index = 0usize;
    while index < inline_segregated_type_cache_capacity(inline_cache_domain) {
        let slot = inline_segregated_type_cache_entry_at(inline_cache_domain, index);
        let entry = *slot;
        if entry.matches_cached_key(layout, cache_key, policy_key) {
            *slot = SegregatedTypeCacheEntry::empty();
            return Some(entry);
        }
        index += 1;
    }
    None
}

unsafe fn pop_segregated_type_cache(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<*mut u8> {
    if !segregated_type_cache_eligible(layout, metadata) {
        record_stats_type_cache_bypass(metadata);
        return None;
    }
    pop_segregated_type_cache_eligible(layout, metadata)
}

unsafe fn pop_segregated_type_cache_eligible(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<*mut u8> {
    let cache_key = type_cache_identity_key(metadata);
    let policy_key = segregated_type_cache_policy_key(metadata);
    pop_segregated_type_cache_eligible_with_key(layout, metadata, cache_key, policy_key)
}

unsafe fn pop_segregated_type_cache_eligible_with_key(
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
    policy_key: u32,
) -> Option<*mut u8> {
    #[cfg(unialloc_target_arm64e)]
    {
        if inline_segregated_type_cache_pop_eligible(metadata) {
            if let Some(entry) =
                arm64e_pop_inline_segregated_type_cache_entry(layout, cache_key, policy_key)
            {
                return Some(finish_popped_segregated_type_cache_entry(
                    layout, metadata, entry,
                ));
            }
        }
        record_stats_type_cache_bypass(metadata);
        return None;
    }

    #[cfg(not(unialloc_target_arm64e))]
    {
        if inline_segregated_type_cache_pop_eligible(metadata) {
            let inline_cache_domain = segregated_type_cache_inline_domain(metadata);
            if let Some(entry) = pop_inline_segregated_type_cache_eligible_with_key(
                layout,
                cache_key,
                policy_key,
                inline_cache_domain,
            ) {
                return Some(finish_popped_segregated_type_cache_entry(
                    layout, metadata, entry,
                ));
            }
        }

        pop_segregated_type_cache_bucket_eligible_with_key(layout, metadata, cache_key, policy_key)
    }
}

unsafe fn pop_segregated_type_cache_bucket_eligible_with_key(
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
    policy_key: u32,
) -> Option<*mut u8> {
    #[cfg(not(feature = "fixed_heap"))]
    let hugepage_cache_is_primary =
        metadata.requests(FLAG_HUGEPAGE_METADATA) && !HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null();

    let (cache, cache_domain) = segregated_type_cache_for_pop(metadata);
    if let Some(entry) =
        pop_segregated_type_cache_from(cache, layout, cache_key, policy_key, cache_domain)
    {
        let ptr = finish_popped_segregated_type_cache_entry(layout, metadata, entry);
        #[cfg(not(feature = "fixed_heap"))]
        if cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE {
            release_empty_hugepage_segregated_type_cache();
        }
        return Some(ptr);
    }

    #[cfg(not(feature = "fixed_heap"))]
    if hugepage_cache_is_primary {
        // A hugepage side-cache request can legitimately fall back to the
        // ordinary static cache when the hugepage mapping is unavailable.  If a
        // later request succeeds in allocating the hugepage cache, those older
        // fallback entries must remain recoverable instead of becoming orphaned
        // cold objects that are never reused.
        if let Some(entry) = pop_segregated_type_cache_from(
            ordinary_segregated_type_cache_mut(),
            layout,
            cache_key,
            policy_key,
            SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
        ) {
            let ptr = finish_popped_segregated_type_cache_entry(layout, metadata, entry);
            release_empty_hugepage_segregated_type_cache();
            return Some(ptr);
        }
        release_empty_hugepage_segregated_type_cache();
    }

    record_stats_type_cache_bypass(metadata);
    None
}

unsafe fn segregated_type_cache_full_bucket_for_replacement(
    cache: &mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
    inline_cache_domain: u8,
    cached_retained_bytes: &mut Option<usize>,
    start: usize,
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
) -> Option<usize> {
    // The probe window is full.  Prefer replacing an entry from the same
    // semantic class so collision pressure from one type/layout/policy does
    // not evict unrelated cached objects from the primary bucket.  A bucket
    // that would violate the retained-byte cap is not a real candidate: keep
    // looking instead of turning a local byte-cap miss into an unnecessary
    // backend deallocation.
    let mut selected = None;
    let mut selected_matches = 0;
    let aggregate_retained_bytes = match *cached_retained_bytes {
        Some(retained_bytes) => retained_bytes,
        None => match segregated_type_cache_trusted_retained_bytes_for_inline_domain(
            &*cache,
            cache_domain,
            inline_cache_domain,
        ) {
            Some(retained_bytes) => {
                *cached_retained_bytes = Some(retained_bytes);
                retained_bytes
            }
            None => return None,
        },
    };
    let mut offset = 0;
    while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
        let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
        if !cache[idx].can_accept_object_size(layout.size()) {
            offset += 1;
            continue;
        }
        match segregated_type_cache_projected_aggregate_retained_bytes(
            aggregate_retained_bytes,
            cache[idx],
            layout.size(),
        ) {
            Some(projected) if projected <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES => {}
            _ => {
                offset += 1;
                continue;
            }
        }

        let matches = cache[idx].matching_entry_count(layout, cache_key, policy_key);
        if selected.is_none() || matches > selected_matches {
            selected = Some(idx);
            selected_matches = matches;
        }
        offset += 1;
    }
    selected
}

#[inline]
unsafe fn inline_segregated_type_cache_has_matching_bucket_entry(
    cache: &mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
    cache_domain: u8,
) -> bool {
    let start = segregated_type_cache_slot_for_key(cache_key, layout);
    let mut observed_corrupt = false;
    let mut checked_hot_idx = None;
    if let Some(idx) = segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain)
    {
        checked_hot_idx = Some(idx);
        record_segregated_type_cache_bucket_probe_step();
        if clear_segregated_type_cache_bucket_if_corrupt(&mut cache[idx], cache_domain) {
            clear_segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain);
            observed_corrupt = true;
        } else if cache[idx].matching_entry_count(layout, cache_key, policy_key) != 0 {
            return true;
        }
    }

    let mut offset = 0;
    while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
        let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
        if segregated_type_cache_probe_already_checked(checked_hot_idx, idx) {
            offset += 1;
            continue;
        }
        record_segregated_type_cache_bucket_probe_step();
        let bucket = &mut cache[idx];
        if clear_segregated_type_cache_bucket_if_corrupt(bucket, cache_domain) {
            clear_segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain);
            observed_corrupt = true;
        } else if bucket.matching_entry_count(layout, cache_key, policy_key) != 0 {
            return true;
        }
        offset += 1;
    }

    observed_corrupt
}

#[inline]
unsafe fn materialize_matching_inline_segregated_type_cache(
    cache: &mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
    cache_domain: u8,
    inline_cache_domain: u8,
    bucket_idx: usize,
    layout: Layout,
    cache_key: u64,
    policy_key: u32,
) {
    let mut index = 0usize;
    while index < inline_segregated_type_cache_capacity(inline_cache_domain) {
        let inline_slot = inline_segregated_type_cache_entry_at(inline_cache_domain, index);
        let entry = *inline_slot;
        if entry.matches_cached_key(layout, cache_key, policy_key) {
            if !try_append_segregated_type_cache_bucket(&mut cache[bucket_idx], cache_domain, entry)
            {
                break;
            }
            *inline_slot = SegregatedTypeCacheEntry::empty();
        }
        index += 1;
    }
}

#[inline]
unsafe fn try_append_segregated_type_cache_bucket(
    bucket: &mut SegregatedTypeCacheBucket,
    cache_domain: u8,
    entry: SegregatedTypeCacheEntry,
) -> bool {
    clear_segregated_type_cache_bucket_if_corrupt(bucket, cache_domain);
    if bucket.count >= SEGREGATED_TYPE_CACHE_DEPTH || !bucket.can_accept_object_size(entry.size) {
        return false;
    }
    let evicted = push_segregated_type_cache_bucket(bucket, cache_domain, entry);
    debug_assert!(evicted.is_none());
    evicted.is_none()
}

#[inline]
unsafe fn push_inline_segregated_type_cache_eligible_with_key(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
    policy_key: u32,
    cache_domain: u8,
    inline_cache_domain: u8,
    cache: &mut [SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS],
) -> bool {
    let mut inline_slot: *mut SegregatedTypeCacheEntry = core::ptr::null_mut();
    let mut inline_slot_index = 0usize;
    let mut index = 0usize;
    while index < inline_segregated_type_cache_capacity(inline_cache_domain) {
        let candidate = inline_segregated_type_cache_entry_at(inline_cache_domain, index);
        let entry = *candidate;
        if inline_cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY
            && entry.matches_cached_key(layout, cache_key, policy_key)
        {
            // Keep multiple objects from one semantic class in the bucket
            // cache, where the existing bounded depth/eviction policy applies.
            // The second ordinary inline slot is reserved for a small
            // alternating pair of distinct classes.
            return false;
        }
        if inline_slot.is_null() && entry.is_empty() {
            inline_slot = candidate;
            inline_slot_index = index;
        }
        index += 1;
    }
    if inline_slot.is_null() {
        return false;
    }
    let occupied_buckets = segregated_type_cache_occupied_bucket_count(cache_domain);
    let start = segregated_type_cache_slot_for_key(cache_key, layout);
    let start_bucket_has_visible_state =
        segregated_type_cache_bucket_has_visible_state(cache[start]);
    let hot_bucket_has_visible_state =
        match segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain) {
            Some(idx) => segregated_type_cache_bucket_has_visible_state(cache[idx]),
            None => false,
        };
    if inline_cache_domain == SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY
        && inline_slot_index != 0
        && (occupied_buckets != 0 || start_bucket_has_visible_state || hot_bucket_has_visible_state)
    {
        // Once the bucket table is active, preserve its hot-bucket and
        // corruption-repair behavior rather than bypassing it through the
        // secondary inline slot.
        return false;
    }
    if occupied_buckets == 0 && !start_bucket_has_visible_state && !hot_bucket_has_visible_state {
        // The common metadata-segregated/PAC/hugepage hot path is one object
        // bouncing through this inline slot.  When no materialized bucket exists
        // in the selected domain, there is nothing to deduplicate against and
        // the aggregate cap reduces to this single incoming object.  Avoiding
        // the bucket probe plus full-table retained-byte scan is what keeps the
        // compiler-supplied semantic fast path a real hot path.
        if inline_segregated_type_cache_retained_bytes(inline_cache_domain)
            .saturating_add(type_cache_retained_bytes_for_layout(layout))
            > MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
        {
            return false;
        }
    } else {
        if inline_segregated_type_cache_has_matching_bucket_entry(
            cache,
            layout,
            cache_key,
            policy_key,
            cache_domain,
        ) {
            return false;
        }
        if !segregated_type_cache_can_grow_inline_aggregate(
            &*cache,
            cache_domain,
            inline_cache_domain,
            layout.size(),
        ) {
            return false;
        }
    }

    *inline_slot = SegregatedTypeCacheEntry {
        cache_key,
        type_id: metadata.type_id,
        policy_key,
        ptr,
        size: layout.size(),
        align: layout.align(),
        auth: metadata_record_auth(ptr, layout, metadata),
        metadata,
    };
    record_stats_type_cache_insert(metadata);
    true
}

unsafe fn push_segregated_type_cache(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Result<Option<SegregatedTypeCacheEntry>, ()> {
    if ptr.is_null() || !segregated_type_cache_eligible(layout, metadata) {
        record_stats_type_cache_bypass(metadata);
        return Err(());
    }
    push_segregated_type_cache_eligible(ptr, layout, metadata)
}

unsafe fn push_segregated_type_cache_eligible(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Result<Option<SegregatedTypeCacheEntry>, ()> {
    let cache_key = type_cache_identity_key(metadata);
    let policy_key = segregated_type_cache_policy_key(metadata);
    push_segregated_type_cache_eligible_with_key(ptr, layout, metadata, cache_key, policy_key)
}

unsafe fn push_segregated_type_cache_eligible_with_key(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    cache_key: u64,
    policy_key: u32,
) -> Result<Option<SegregatedTypeCacheEntry>, ()> {
    #[cfg(unialloc_target_arm64e)]
    {
        if inline_segregated_type_cache_eligible_for_classified(metadata)
            && arm64e_push_inline_segregated_type_cache_entry(
                ptr, layout, metadata, cache_key, policy_key,
            )
        {
            return Ok(None);
        }
        record_stats_type_cache_bypass(metadata);
        return Err(());
    }

    #[cfg(not(unialloc_target_arm64e))]
    {
        #[cfg(not(feature = "fixed_heap"))]
        if metadata.requests(FLAG_HUGEPAGE_METADATA) && HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
            // Avoid allocating a hugepage-sized side-cache mapping for the common
            // one- or two-object reuse case.  We still scan the ordinary fallback bucket
            // table so older entries created when hugepage mapping was unavailable
            // remain the single source of truth for their class.  The inline entries
            // are hugepage-domain local, so an ordinary metadata hot entry does not
            // force either of the first two hugepage frees to allocate the bucket
            // mapping.
            let cache = ordinary_segregated_type_cache_mut();
            if push_inline_segregated_type_cache_eligible_with_key(
                ptr,
                layout,
                metadata,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                cache,
            ) {
                return Ok(None);
            }
        }

        let (cache, cache_domain) = segregated_type_cache_for_push(metadata);
        let inline_cache_domain = segregated_type_cache_inline_domain(metadata);
        if inline_segregated_type_cache_eligible_for_classified(metadata) {
            if push_inline_segregated_type_cache_eligible_with_key(
                ptr,
                layout,
                metadata,
                cache_key,
                policy_key,
                cache_domain,
                inline_cache_domain,
                cache,
            ) {
                return Ok(None);
            }
        }

        let start = segregated_type_cache_slot_for_key(cache_key, layout);
        let mut bucket_idx = start;
        let mut found_available_bucket = false;
        let mut checked_hot_idx = None;
        // A push may inspect several candidate buckets and then perform a final
        // acceptance check.  The aggregate retained-byte cap is still validated
        // from real TLS cache contents, but a single mutation-free push should
        // not rescan the whole table for every candidate.  Any corruption repair
        // below invalidates the snapshot so the next check recomputes it.
        let mut aggregate_retained_bytes = None;
        if let Some(idx) =
            segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain)
        {
            checked_hot_idx = Some(idx);
            record_segregated_type_cache_bucket_probe_step();
            if clear_segregated_type_cache_bucket_if_corrupt(&mut cache[idx], cache_domain) {
                aggregate_retained_bytes = None;
                clear_segregated_type_cache_hot_bucket(layout, cache_key, policy_key, cache_domain);
                if cache[idx].can_accept_object_size(layout.size())
                    && segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
                        &*cache,
                        cache_domain,
                        inline_cache_domain,
                        &mut aggregate_retained_bytes,
                        idx,
                        layout.size(),
                    )
                {
                    bucket_idx = idx;
                    found_available_bucket = true;
                }
            } else {
                let bucket_matches =
                    cache[idx].matching_entry_count(layout, cache_key, policy_key) != 0;
                let bucket_can_accept = cache[idx].can_accept_object_size(layout.size());
                if bucket_matches && !bucket_can_accept {
                    record_stats_type_cache_bypass(metadata);
                    return Err(());
                }
                if cache[idx].count < SEGREGATED_TYPE_CACHE_DEPTH
                    && bucket_can_accept
                    && segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
                        &*cache,
                        cache_domain,
                        inline_cache_domain,
                        &mut aggregate_retained_bytes,
                        idx,
                        layout.size(),
                    )
                {
                    bucket_idx = idx;
                    found_available_bucket = true;
                }
            }
        }
        let mut offset = 0;
        if !found_available_bucket {
            while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
                let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
                if segregated_type_cache_probe_already_checked(checked_hot_idx, idx) {
                    offset += 1;
                    continue;
                }
                record_segregated_type_cache_bucket_probe_step();
                if clear_segregated_type_cache_bucket_if_corrupt(&mut cache[idx], cache_domain) {
                    aggregate_retained_bytes = None;
                    clear_segregated_type_cache_hot_bucket(
                        layout,
                        cache_key,
                        policy_key,
                        cache_domain,
                    );
                    if cache[idx].can_accept_object_size(layout.size())
                        && segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
                            &*cache,
                            cache_domain,
                            inline_cache_domain,
                            &mut aggregate_retained_bytes,
                            idx,
                            layout.size(),
                        )
                    {
                        bucket_idx = idx;
                        found_available_bucket = true;
                        break;
                    }
                }
                let bucket_matches =
                    cache[idx].matching_entry_count(layout, cache_key, policy_key) != 0;
                let bucket_can_accept = cache[idx].can_accept_object_size(layout.size());
                if bucket_matches && !bucket_can_accept {
                    record_stats_type_cache_bypass(metadata);
                    return Err(());
                }
                if cache[idx].count < SEGREGATED_TYPE_CACHE_DEPTH
                    && bucket_can_accept
                    && segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
                        &*cache,
                        cache_domain,
                        inline_cache_domain,
                        &mut aggregate_retained_bytes,
                        idx,
                        layout.size(),
                    )
                {
                    bucket_idx = idx;
                    found_available_bucket = true;
                    break;
                }
                offset += 1;
            }
        }
        if !found_available_bucket {
            match segregated_type_cache_full_bucket_for_replacement(
                cache,
                cache_domain,
                inline_cache_domain,
                &mut aggregate_retained_bytes,
                start,
                layout,
                cache_key,
                policy_key,
            ) {
                Some(idx) => bucket_idx = idx,
                None => {
                    record_stats_type_cache_bypass(metadata);
                    return Err(());
                }
            }
        }

        if !cache[bucket_idx].can_accept_object_size(layout.size()) {
            record_stats_type_cache_bypass(metadata);
            return Err(());
        }
        if !segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
            &*cache,
            cache_domain,
            inline_cache_domain,
            &mut aggregate_retained_bytes,
            bucket_idx,
            layout.size(),
        ) {
            record_stats_type_cache_bypass(metadata);
            return Err(());
        }
        let bucket = &mut cache[bucket_idx];
        let evicted = push_segregated_type_cache_bucket(
            bucket,
            cache_domain,
            SegregatedTypeCacheEntry {
                cache_key,
                type_id: metadata.type_id,
                policy_key,
                ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(ptr, layout, metadata),
                metadata,
            },
        );
        remember_segregated_type_cache_hot_bucket(
            layout,
            cache_key,
            policy_key,
            cache_domain,
            bucket_idx,
        );
        if evicted.is_none() {
            materialize_matching_inline_segregated_type_cache(
                cache,
                cache_domain,
                inline_cache_domain,
                bucket_idx,
                layout,
                cache_key,
                policy_key,
            );
        }
        record_stats_type_cache_insert(metadata);
        Ok(evicted)
    }
}

unsafe fn pop_plain_semantic_type_cache_eligible(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<*mut u8> {
    let cache_key = type_cache_identity_key(metadata);
    if let Some(ptr) = pop_inline_type_cache_eligible_with_key(layout, metadata, cache_key) {
        return Some(ptr);
    }
    pop_type_cache_eligible_with_key(layout, metadata, cache_key)
}

unsafe fn pop_semantic_type_cache(layout: Layout, metadata: AllocationMetadata) -> Option<*mut u8> {
    match semantic_type_cache_class(layout, metadata) {
        Some(SemanticTypeCacheClass::Segregated) => {
            pop_segregated_type_cache_eligible(layout, metadata)
        }
        Some(SemanticTypeCacheClass::Plain) => {
            pop_plain_semantic_type_cache_eligible(layout, metadata)
        }
        None => {
            record_stats_type_cache_bypass(metadata);
            None
        }
    }
}

unsafe fn pop_compiler_type_metadata_cache(
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<*mut u8> {
    match compiler_type_metadata_cache_class(layout, metadata) {
        Some(SemanticTypeCacheClass::Segregated) => {
            pop_segregated_type_cache_eligible(layout, metadata)
        }
        Some(SemanticTypeCacheClass::Plain) => {
            pop_plain_semantic_type_cache_eligible(layout, metadata)
        }
        None => {
            record_stats_type_cache_bypass(metadata);
            None
        }
    }
}

unsafe fn push_plain_semantic_type_cache_eligible(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    let cache_key = type_cache_identity_key(metadata);
    if push_inline_type_cache_eligible_with_key(ptr, layout, metadata, cache_key) {
        true
    } else {
        push_type_cache_eligible_with_key(ptr, layout, metadata, cache_key)
    }
}

unsafe fn cache_compiler_type_metadata_free(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    if ptr.is_null() {
        record_stats_type_cache_bypass(metadata);
        return false;
    }
    match compiler_type_metadata_cache_class(layout, metadata) {
        Some(SemanticTypeCacheClass::Segregated) => {
            match push_segregated_type_cache_eligible(ptr, layout, metadata) {
                Ok(evicted) => {
                    if let Some(slot) = evicted {
                        release_evicted_segregated_type_cache_entry(alloc, slot);
                    }
                    true
                }
                Err(()) => false,
            }
        }
        Some(SemanticTypeCacheClass::Plain) => {
            push_plain_semantic_type_cache_eligible(ptr, layout, metadata)
        }
        None => {
            record_stats_type_cache_bypass(metadata);
            false
        }
    }
}

unsafe fn cache_semantic_free(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> bool {
    if ptr.is_null() {
        record_stats_type_cache_bypass(metadata);
        return false;
    }
    match semantic_type_cache_class(layout, metadata) {
        Some(SemanticTypeCacheClass::Segregated) => {
            match push_segregated_type_cache_eligible(ptr, layout, metadata) {
                Ok(evicted) => {
                    if let Some(slot) = evicted {
                        release_evicted_segregated_type_cache_entry(alloc, slot);
                    }
                    true
                }
                Err(()) => false,
            }
        }
        Some(SemanticTypeCacheClass::Plain) => {
            push_plain_semantic_type_cache_eligible(ptr, layout, metadata)
        }
        None => {
            record_stats_type_cache_bypass(metadata);
            false
        }
    }
}

#[inline]
fn checked_side_table_layout(size: usize, align: usize, kind: &'static str) -> Layout {
    match Layout::from_size_align(size, align) {
        Ok(layout) => layout,
        Err(_) => panic!("{} side-table layout corrupt", kind),
    }
}

unsafe fn release_evicted_segregated_type_cache_entry(
    alloc: &RustAllocator,
    slot: SegregatedTypeCacheEntry,
) {
    let evicted_layout = checked_side_table_layout(slot.size, slot.align, "segregated metadata");
    verify_metadata_record_auth(
        slot.ptr,
        evicted_layout,
        slot.metadata,
        slot.metadata,
        slot.auth,
    );
    if slot.metadata.requests(FLAG_FORCE_INITIALIZE) {
        core::ptr::write_bytes(slot.ptr, 0, evicted_layout.size());
    }
    alloc.dealloc_raw(slot.ptr, evicted_layout);
}

#[inline]
fn compute_metadata_record_auth(
    seed: u64,
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> u64 {
    #[cfg(test)]
    METADATA_RECORD_AUTH_COMPUTATIONS.fetch_add(1, Ordering::Relaxed);
    let hash = fnv1a_mix(seed, &(ptr as usize).to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
    non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true))
}

#[inline]
fn derive_metadata_record_auth(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) -> u64 {
    let seed = metadata_record_auth_seed();
    #[cfg(not(unialloc_target_arm64e))]
    unsafe {
        let hot = METADATA_RECORD_AUTH_HOT_SLOT;
        if hot.matches(seed, ptr, layout, metadata) {
            return hot.auth;
        }
    }

    let auth = compute_metadata_record_auth(seed, ptr, layout, metadata);
    #[cfg(not(unialloc_target_arm64e))]
    unsafe {
        METADATA_RECORD_AUTH_HOT_SLOT = MetadataRecordAuthHotSlot {
            seed,
            ptr: ptr as usize,
            size: layout.size(),
            align: layout.align(),
            metadata,
            auth,
            active: true,
        };
    }
    auth
}

#[inline]
fn metadata_auth_cookie() -> u64 {
    let cached = METADATA_AUTH_COOKIE.load(Ordering::Relaxed);
    if cached != 0 {
        return cached;
    }

    let stack_anchor = 0usize;
    let mut hash = fnv1a_mix(FNV1A_OFFSET, b"semantic-metadata-auth-cookie-v1");
    let stats_addr = &SEMANTIC_STATS as *const SemanticStats as usize;
    let slow_path_addr = &SEMANTIC_SLOW_PATH_FLAGS as *const AtomicUsize as usize;
    let type_stats_addr = &SEMANTIC_TYPE_STATS as *const _ as usize;
    let stack_addr = &stack_anchor as *const usize as usize;
    hash = fnv1a_mix(hash, &stats_addr.to_le_bytes());
    hash = fnv1a_mix(hash, &slow_path_addr.to_le_bytes());
    hash = fnv1a_mix(hash, &type_stats_addr.to_le_bytes());
    hash = fnv1a_mix(hash, &stack_addr.to_le_bytes());

    let cookie = non_zero_hash(hash);
    match METADATA_AUTH_COOKIE.compare_exchange(0, cookie, Ordering::Relaxed, Ordering::Relaxed) {
        Ok(_) => cookie,
        Err(existing) => existing,
    }
}

#[inline]
fn keyed_integrity_hash(domain: &[u8]) -> u64 {
    let hash = fnv1a_mix(FNV1A_OFFSET, domain);
    fnv1a_mix(hash, &metadata_auth_cookie().to_le_bytes())
}

#[inline]
fn metadata_record_auth_seed() -> u64 {
    let cached = METADATA_RECORD_AUTH_SEED.load(Ordering::Relaxed);
    if cached != 0 {
        return cached;
    }

    #[cfg(test)]
    METADATA_RECORD_AUTH_SEED_DERIVATIONS.fetch_add(1, Ordering::Relaxed);
    let seed = non_zero_hash(keyed_integrity_hash(b"semantic-metadata-auth-v1"));
    match METADATA_RECORD_AUTH_SEED.compare_exchange(0, seed, Ordering::Relaxed, Ordering::Relaxed)
    {
        Ok(_) => seed,
        Err(existing) => existing,
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn derive_metadata_record_pac_context(layout: Layout, metadata: AllocationMetadata) -> usize {
    let hash = keyed_integrity_hash(b"semantic-metadata-pac-context-v1");
    let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
    non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true)) as usize
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum MetadataPacAuthBackend {
    HardwarePac,
    SoftwareHashFallback,
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn metadata_pac_auth_backend() -> MetadataPacAuthBackend {
    if crate::pal::arch::pac::context_binding_available() {
        MetadataPacAuthBackend::HardwarePac
    } else {
        MetadataPacAuthBackend::SoftwareHashFallback
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn metadata_record_pac_auth(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) -> u64 {
    if metadata_pac_auth_backend() == MetadataPacAuthBackend::SoftwareHashFallback {
        record_stats_metadata_pac_software_fallback_sign();
        return derive_metadata_record_auth(ptr, layout, metadata);
    }

    let context = derive_metadata_record_pac_context(layout, metadata);
    if let Some(signed) = crate::pal::arch::pac::sign_context_bound_pointer(ptr as usize, context) {
        record_stats_metadata_pac_auth_sign();
        signed as u64
    } else {
        // Defensive race/fail-closed path: if the runtime PAC probe becomes
        // unavailable between backend selection and signing, do not emit an
        // unauthenticated raw pointer while claiming hardware PAC coverage.
        record_stats_metadata_pac_software_fallback_sign();
        derive_metadata_record_auth(ptr, layout, metadata)
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn metadata_record_pac_auth_valid(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    auth: u64,
) -> bool {
    if metadata_pac_auth_backend() == MetadataPacAuthBackend::SoftwareHashFallback {
        let valid = auth == derive_metadata_record_auth(ptr, layout, metadata);
        record_stats_metadata_pac_software_fallback_verification(valid);
        return valid;
    }

    if auth == 0 {
        record_stats_metadata_pac_auth_verification(false);
        return false;
    }
    let signed = auth as usize;
    let context = derive_metadata_record_pac_context(layout, metadata);
    let valid = crate::pal::arch::pac::authenticate_context_bound_pointer(signed, context)
        == Some(ptr as usize);
    record_stats_metadata_pac_auth_verification(valid);
    valid
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn metadata_record_integrity_auth(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> u64 {
    if metadata.requests(FLAG_POINTER_AUTH) {
        metadata_record_pac_auth(ptr, layout, metadata)
    } else {
        derive_metadata_record_auth(ptr, layout, metadata)
    }
}

#[cfg(not(all(feature = "pac", target_arch = "aarch64")))]
#[inline]
fn metadata_record_integrity_auth(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> u64 {
    derive_metadata_record_auth(ptr, layout, metadata)
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn metadata_record_integrity_auth_valid(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    auth: u64,
) -> bool {
    if metadata.requests(FLAG_POINTER_AUTH) {
        metadata_record_pac_auth_valid(ptr, layout, metadata, auth)
    } else {
        auth == derive_metadata_record_auth(ptr, layout, metadata)
    }
}

#[cfg(not(all(feature = "pac", target_arch = "aarch64")))]
#[inline]
fn metadata_record_integrity_auth_valid(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    auth: u64,
) -> bool {
    auth == derive_metadata_record_auth(ptr, layout, metadata)
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
#[repr(C)]
pub struct MetadataPointerAuthRuntimeProbe {
    pub backend_hardware_pac: bool,
    pub backend_software_fallback: bool,
    pub signed_changed: bool,
    pub correct_context_valid: bool,
    pub wrong_layout_rejected: bool,
    pub wrong_metadata_rejected: bool,
    pub auth_nonzero: bool,
}

/// Run the allocator metadata-auth primitive on a caller-owned allocation.
///
/// This intentionally does not touch type-cache or recovery-record TLS.  It is
/// used by arm64e no_std probes where the current Rust toolchain can execute
/// PAC instructions but faults in Rust TLS descriptors before the ordinary
/// type-isolated side-cache path can run.  A successful result proves the same
/// metadata PAC sign/auth primitive used by side-cache records, not full
/// side-cache reuse.
#[inline]
pub unsafe fn metadata_pointer_auth_runtime_probe(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> MetadataPointerAuthRuntimeProbe {
    #[cfg(all(feature = "pac", target_arch = "aarch64"))]
    {
        let auth = metadata_record_pac_auth(ptr, layout, metadata);
        let correct_context_valid = metadata_record_pac_auth_valid(ptr, layout, metadata, auth);

        #[cfg(unialloc_target_arm64e)]
        let (wrong_layout_rejected, wrong_metadata_rejected) = {
            // arm64e reports wrong-context PAC authentication as
            // EXC_ARM_PAC_FAIL/SIGBUS at the AUT instruction.  Keep this
            // positive runtime probe alive and let the evaluator collect a
            // separate expected-signal negative run for wrong-context evidence.
            let positive_pac_path =
                auth != 0 && auth != derive_metadata_record_auth(ptr, layout, metadata);
            (
                positive_pac_path && correct_context_valid,
                positive_pac_path && correct_context_valid,
            )
        };
        #[cfg(not(unialloc_target_arm64e))]
        let (wrong_layout_rejected, wrong_metadata_rejected) = {
            let wrong_layout = Layout::from_size_align(
                layout.size().saturating_add(core::mem::size_of::<usize>()),
                layout.align(),
            )
            .unwrap_or(layout);
            let wrong_metadata = metadata.with_callsite(metadata.callsite ^ 0x5a5a_a5a5_1111_2222);
            (
                !metadata_record_pac_auth_valid(ptr, wrong_layout, metadata, auth),
                !metadata_record_pac_auth_valid(ptr, layout, wrong_metadata, auth),
            )
        };
        let backend_hardware_pac =
            metadata_pac_auth_backend() == MetadataPacAuthBackend::HardwarePac;
        MetadataPointerAuthRuntimeProbe {
            backend_hardware_pac,
            backend_software_fallback: !backend_hardware_pac,
            signed_changed: auth != derive_metadata_record_auth(ptr, layout, metadata),
            correct_context_valid,
            wrong_layout_rejected,
            wrong_metadata_rejected,
            auth_nonzero: auth != 0,
        }
    }
    #[cfg(not(all(feature = "pac", target_arch = "aarch64")))]
    {
        let auth = metadata_record_integrity_auth(ptr, layout, metadata);
        MetadataPointerAuthRuntimeProbe {
            backend_hardware_pac: false,
            backend_software_fallback: true,
            signed_changed: false,
            correct_context_valid: metadata_record_integrity_auth_valid(
                ptr, layout, metadata, auth,
            ),
            wrong_layout_rejected: !metadata_record_integrity_auth_valid(
                ptr,
                Layout::from_size_align(
                    layout.size().saturating_add(core::mem::size_of::<usize>()),
                    layout.align(),
                )
                .unwrap_or(layout),
                metadata,
                auth,
            ),
            wrong_metadata_rejected: !metadata_record_integrity_auth_valid(
                ptr,
                layout,
                metadata.with_callsite(metadata.callsite ^ 0x5a5a_a5a5_1111_2222),
                auth,
            ),
            auth_nonzero: auth != 0,
        }
    }
}

#[inline]
fn metadata_record_auth(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) -> u64 {
    if metadata_integrity_required(metadata) {
        metadata_record_integrity_auth(ptr, layout, metadata)
    } else {
        0
    }
}

fn verify_metadata_record_auth(
    ptr: *mut u8,
    layout: Layout,
    requested_metadata: AllocationMetadata,
    stored_metadata: AllocationMetadata,
    auth: u64,
) {
    if !metadata_record_auth_is_valid(ptr, layout, requested_metadata, stored_metadata, auth) {
        panic!("metadata integrity check failed");
    }
}

#[inline]
fn metadata_record_auth_is_valid(
    ptr: *mut u8,
    layout: Layout,
    requested_metadata: AllocationMetadata,
    stored_metadata: AllocationMetadata,
    auth: u64,
) -> bool {
    if metadata_integrity_required(requested_metadata)
        || metadata_integrity_required(stored_metadata)
        || auth != 0
    {
        metadata_record_integrity_auth_valid(ptr, layout, stored_metadata, auth)
    } else {
        true
    }
}

#[inline]
fn derive_auto_allocation_record_auth(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> u64 {
    let hash = keyed_integrity_hash(b"semantic-auto-allocation-record-v1");
    let hash = fnv1a_mix(hash, &(ptr as usize).to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
    non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true))
}

#[inline]
fn derive_memory_tag(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) -> u64 {
    let hash = keyed_integrity_hash(b"semantic-memory-tag-v1");
    let hash = fnv1a_mix(hash, &(ptr as usize).to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
    let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
    non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true))
}

#[inline]
fn memory_tag_typed_metadata_matches(
    stored: AllocationMetadata,
    requested: AllocationMetadata,
) -> bool {
    // The stored tag is still derived from the allocation callsite, so tag
    // tampering remains bound to the exact recorded allocation metadata.
    // Deallocation metadata, however, may come from a compiler-inserted Drop
    // site with a different callsite hash.  Treat callsite as coverage/debug
    // context here, matching the scoped deallocation recovery path.
    stored.type_id == requested.type_id
        && stored.module_id == requested.module_id
        && stored.flags == requested.flags
        && stored.lifetime_hint == requested.lifetime_hint
        && stored.placement_hint == requested.placement_hint
}

#[inline]
fn memory_tag_hash(ptr: *mut u8) -> usize {
    let mut value = (ptr as usize) >> 4;
    value ^= value >> 16;
    value ^= value >> 32;
    value.wrapping_mul(0x9e37_79b1usize)
}

#[inline]
fn memory_tag_fast_index(ptr: *mut u8) -> usize {
    memory_tag_hash(ptr) & (MEMORY_TAG_FAST_SLOTS - 1)
}

#[inline]
fn global_memory_tag_shard_and_slot(ptr: *mut u8) -> (usize, usize) {
    let slot = memory_tag_fast_index(ptr);
    (
        slot / GLOBAL_MEMORY_TAG_SHARD_SLOTS,
        slot & (GLOBAL_MEMORY_TAG_SHARD_SLOTS - 1),
    )
}

#[inline]
fn global_memory_tag_shard(ptr: *mut u8) -> usize {
    global_memory_tag_shard_and_slot(ptr).0 & GLOBAL_MEMORY_TAG_SHARD_MASK
}

#[inline]
fn global_memory_tag_fast_index(ptr: *mut u8) -> usize {
    global_memory_tag_shard_and_slot(ptr).1
}

#[inline]
fn global_memory_tag_table_for_ptr(ptr: *mut u8) -> &'static Mutex<GlobalMemoryTagTable> {
    &GLOBAL_MEMORY_TAGS[global_memory_tag_shard(ptr)]
}

#[inline]
fn memory_tag_requires_global_visibility(metadata: AllocationMetadata) -> bool {
    metadata.placement_hint & PLACEMENT_HINT_CROSS_THREAD_RECOVERY != 0
}

#[inline]
unsafe fn remember_memory_tag_hot_slot(ptr: *mut u8, slot_idx: usize) {
    MEMORY_TAG_HOT_SLOT = MemoryTagHotSlot {
        ptr: ptr as usize,
        slot_idx,
    };
}

#[inline]
unsafe fn clear_memory_tag_hot_slot(ptr: *mut u8) {
    if MEMORY_TAG_HOT_SLOT.ptr == ptr as usize {
        MEMORY_TAG_HOT_SLOT = MemoryTagHotSlot::empty();
    }
}

#[inline]
unsafe fn memory_tag_hot_slot(ptr: *mut u8) -> Option<usize> {
    let hot = MEMORY_TAG_HOT_SLOT;
    if hot.ptr == ptr as usize {
        Some(hot.slot_idx & (MEMORY_TAG_FAST_SLOTS - 1))
    } else {
        None
    }
}

#[inline]
fn remember_global_memory_tag_hot_slot(
    table: &mut GlobalMemoryTagTable,
    ptr: *mut u8,
    slot_idx: usize,
) {
    table.hot = MemoryTagHotSlot {
        ptr: ptr as usize,
        slot_idx,
    };
}

#[inline]
fn clear_global_memory_tag_hot_slot(table: &mut GlobalMemoryTagTable, ptr: *mut u8) {
    if table.hot.ptr == ptr as usize {
        table.hot = MemoryTagHotSlot::empty();
    }
}

#[inline]
fn global_memory_tag_hot_slot(table: &GlobalMemoryTagTable, ptr: *mut u8) -> Option<usize> {
    if table.hot.ptr == ptr as usize {
        Some(table.hot.slot_idx & (GLOBAL_MEMORY_TAG_SHARD_SLOTS - 1))
    } else {
        None
    }
}

unsafe fn find_memory_tag_slot(ptr: *mut u8) -> Option<*mut TaggedAllocation> {
    if let Some(idx) = memory_tag_hot_slot(ptr) {
        let slot = &mut MEMORY_TAGS[idx];
        if slot.ptr == ptr {
            return Some(slot as *mut TaggedAllocation);
        }
        clear_memory_tag_hot_slot(ptr);
    }

    let start = memory_tag_fast_index(ptr);
    let mut offset = 0;
    while offset < MEMORY_TAG_FAST_PROBE_LIMIT {
        let idx = (start + offset) & (MEMORY_TAG_FAST_SLOTS - 1);
        let slot = &mut MEMORY_TAGS[idx];
        if slot.ptr == ptr {
            remember_memory_tag_hot_slot(ptr, idx);
            return Some(slot as *mut TaggedAllocation);
        }
        offset += 1;
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        let mut page = MEMORY_TAG_OVERFLOW;
        while !page.is_null() {
            let page_ref = &mut *page;
            let mut entry_idx = 0;
            while entry_idx < MEMORY_TAG_PAGE_SLOTS {
                let slot = &mut page_ref.entries[entry_idx];
                if slot.ptr == ptr {
                    return Some(slot as *mut TaggedAllocation);
                }
                entry_idx += 1;
            }
            page = page_ref.next;
        }
    }

    None
}

#[inline]
fn current_thread_memory_tag_records_active() -> bool {
    unsafe { MEMORY_TAG_RECORD_COUNT != 0 }
}

#[inline]
fn global_memory_tag_records_active() -> bool {
    GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed) != 0
}

fn find_global_memory_tag_slot_index_in_fast_table(
    fast: &[TaggedAllocation; GLOBAL_MEMORY_TAG_SHARD_SLOTS],
    ptr: *mut u8,
) -> Option<usize> {
    let start = global_memory_tag_fast_index(ptr);
    let mut offset = 0;
    while offset < GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT {
        let idx = (start + offset) & (GLOBAL_MEMORY_TAG_SHARD_SLOTS - 1);
        if fast[idx].ptr == ptr {
            return Some(idx);
        }
        offset += 1;
    }
    None
}

#[cfg(not(feature = "fixed_heap"))]
fn find_memory_tag_slot_in_overflow(
    overflow: *mut MemoryTagPage,
    ptr: *mut u8,
) -> Option<*mut TaggedAllocation> {
    let mut page = overflow;
    while !page.is_null() {
        let page_ref = unsafe { &mut *page };
        let mut entry_idx = 0;
        while entry_idx < MEMORY_TAG_PAGE_SLOTS {
            let slot = &mut page_ref.entries[entry_idx];
            if slot.ptr == ptr {
                return Some(slot as *mut TaggedAllocation);
            }
            entry_idx += 1;
        }
        page = page_ref.next;
    }
    None
}

#[cfg(not(feature = "fixed_heap"))]
fn first_empty_memory_tag_slot_in_overflow(overflow: *mut MemoryTagPage) -> *mut TaggedAllocation {
    let mut page = overflow;
    while !page.is_null() {
        let page_ref = unsafe { &mut *page };
        let mut entry_idx = 0;
        while entry_idx < MEMORY_TAG_PAGE_SLOTS {
            let slot = &mut page_ref.entries[entry_idx];
            if slot.is_empty() {
                return slot as *mut TaggedAllocation;
            }
            entry_idx += 1;
        }
        page = page_ref.next;
    }
    core::ptr::null_mut()
}

#[cfg(not(feature = "fixed_heap"))]
fn memory_tag_overflow_page_is_empty(page: &MemoryTagPage) -> bool {
    page.entries.iter().all(|entry| entry.is_empty())
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn release_empty_memory_tag_overflow_pages(head: *mut *mut MemoryTagPage) {
    let mut prev: *mut MemoryTagPage = core::ptr::null_mut();
    let mut page = *head;

    while !page.is_null() {
        let next = (*page).next;
        if memory_tag_overflow_page_is_empty(&*page) {
            if prev.is_null() {
                *head = next;
            } else {
                (*prev).next = next;
            }
            system_alloc::munmap(page as *mut u8, size_of::<MemoryTagPage>());
        } else {
            prev = page;
        }
        page = next;
    }
}

fn find_global_memory_tag_slot(
    table: &mut GlobalMemoryTagTable,
    ptr: *mut u8,
) -> Option<*mut TaggedAllocation> {
    if let Some(idx) = global_memory_tag_hot_slot(&*table, ptr) {
        if table.fast[idx].ptr == ptr {
            return Some(&mut table.fast[idx] as *mut TaggedAllocation);
        }
        clear_global_memory_tag_hot_slot(table, ptr);
    }
    if let Some(idx) = find_global_memory_tag_slot_index_in_fast_table(&table.fast, ptr) {
        remember_global_memory_tag_hot_slot(table, ptr, idx);
        return Some(&mut table.fast[idx] as *mut TaggedAllocation);
    }
    #[cfg(not(feature = "fixed_heap"))]
    {
        return find_memory_tag_slot_in_overflow(table.overflow, ptr);
    }
    #[cfg(feature = "fixed_heap")]
    {
        None
    }
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
enum MemoryTagReservationError {
    DuplicateAllocationRecord,
    SideTableExhausted,
}

struct MemoryTagSlotReservation {
    slot: *mut TaggedAllocation,
    fast_idx: Option<usize>,
}

fn reserve_global_memory_tag_slot_for(
    table: &mut GlobalMemoryTagTable,
    ptr: *mut u8,
) -> Result<MemoryTagSlotReservation, MemoryTagReservationError> {
    let start = global_memory_tag_fast_index(ptr);
    let mut first_empty: *mut TaggedAllocation = core::ptr::null_mut();
    let mut first_empty_fast_idx = None;
    let mut offset = 0;
    while offset < GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT {
        let idx = (start + offset) & (GLOBAL_MEMORY_TAG_SHARD_SLOTS - 1);
        let slot = &mut table.fast[idx];
        if slot.ptr == ptr {
            return Err(MemoryTagReservationError::DuplicateAllocationRecord);
        }
        if first_empty.is_null() && slot.is_empty() {
            first_empty = slot as *mut TaggedAllocation;
            first_empty_fast_idx = Some(idx);
        }
        offset += 1;
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        if find_memory_tag_slot_in_overflow(table.overflow, ptr).is_some() {
            return Err(MemoryTagReservationError::DuplicateAllocationRecord);
        }
        if first_empty.is_null() {
            first_empty = first_empty_memory_tag_slot_in_overflow(table.overflow);
            first_empty_fast_idx = None;
        }
    }

    if !first_empty.is_null() {
        return Ok(MemoryTagSlotReservation {
            slot: first_empty,
            fast_idx: first_empty_fast_idx,
        });
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        let page = unsafe { allocate_memory_tag_page() };
        if !page.is_null() {
            unsafe {
                (*page).next = table.overflow;
                table.overflow = page;
                return Ok(MemoryTagSlotReservation {
                    slot: &mut (*page).entries[0] as *mut TaggedAllocation,
                    fast_idx: None,
                });
            }
        }
    }

    Err(MemoryTagReservationError::SideTableExhausted)
}

unsafe fn reserve_memory_tag_slot_for(
    ptr: *mut u8,
) -> Result<MemoryTagSlotReservation, MemoryTagReservationError> {
    let start = memory_tag_fast_index(ptr);
    let mut first_empty: *mut TaggedAllocation = core::ptr::null_mut();
    let mut first_empty_fast_idx = None;
    let mut offset = 0;
    while offset < MEMORY_TAG_FAST_PROBE_LIMIT {
        let idx = (start + offset) & (MEMORY_TAG_FAST_SLOTS - 1);
        let slot = &mut MEMORY_TAGS[idx];
        if slot.ptr == ptr {
            return Err(MemoryTagReservationError::DuplicateAllocationRecord);
        }
        if first_empty.is_null() && slot.is_empty() {
            first_empty = slot as *mut TaggedAllocation;
            first_empty_fast_idx = Some(idx);
        }
        offset += 1;
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        let mut page = MEMORY_TAG_OVERFLOW;
        while !page.is_null() {
            let page_ref = &mut *page;
            let mut entry_idx = 0;
            while entry_idx < MEMORY_TAG_PAGE_SLOTS {
                let slot = &mut page_ref.entries[entry_idx];
                if slot.ptr == ptr {
                    return Err(MemoryTagReservationError::DuplicateAllocationRecord);
                }
                if first_empty.is_null() && slot.is_empty() {
                    first_empty = slot as *mut TaggedAllocation;
                    first_empty_fast_idx = None;
                }
                entry_idx += 1;
            }
            page = page_ref.next;
        }
    }

    if !first_empty.is_null() {
        return Ok(MemoryTagSlotReservation {
            slot: first_empty,
            fast_idx: first_empty_fast_idx,
        });
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        let page = allocate_memory_tag_page();
        if !page.is_null() {
            (*page).next = MEMORY_TAG_OVERFLOW;
            MEMORY_TAG_OVERFLOW = page;
            return Ok(MemoryTagSlotReservation {
                slot: &mut (*page).entries[0] as *mut TaggedAllocation,
                fast_idx: None,
            });
        }
    }

    Err(MemoryTagReservationError::SideTableExhausted)
}

fn record_global_memory_tagged_allocation(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Result<(), MemoryTagReservationError> {
    let mut table = global_memory_tag_table_for_ptr(ptr).lock();
    let reservation = reserve_global_memory_tag_slot_for(&mut *table, ptr)?;
    unsafe {
        *reservation.slot = TaggedAllocation {
            ptr,
            size: layout.size(),
            align: layout.align(),
            tag: derive_memory_tag(ptr, layout, metadata),
            metadata,
        };
    }
    if let Some(idx) = reservation.fast_idx {
        remember_global_memory_tag_hot_slot(&mut *table, ptr, idx);
    }
    GLOBAL_MEMORY_TAG_RECORD_COUNT.fetch_add(1, Ordering::Relaxed);
    Ok(())
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn allocate_memory_tag_page() -> *mut MemoryTagPage {
    let prot = system_alloc::prots::get_prot(true, true, false);
    let page = system_alloc::mmap(size_of::<MemoryTagPage>(), prot) as *mut MemoryTagPage;
    if page.is_null() || page as usize == usize::MAX {
        return core::ptr::null_mut();
    }
    page.write(MemoryTagPage::empty());
    page
}

unsafe fn record_memory_tagged_allocation(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Result<(), MemoryTagReservationError> {
    if ptr.is_null() || !metadata.requests(FLAG_MEMORY_TAGGING) {
        return Ok(());
    }

    if memory_tag_requires_global_visibility(metadata) {
        return record_global_memory_tagged_allocation(ptr, layout, metadata);
    }

    let reservation = reserve_memory_tag_slot_for(ptr)?;
    (*reservation.slot) = TaggedAllocation {
        ptr,
        size: layout.size(),
        align: layout.align(),
        tag: derive_memory_tag(ptr, layout, metadata),
        metadata,
    };
    if let Some(idx) = reservation.fast_idx {
        remember_memory_tag_hot_slot(ptr, idx);
    }
    MEMORY_TAG_RECORD_COUNT = MEMORY_TAG_RECORD_COUNT.saturating_add(1);
    Ok(())
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
enum MemoryTagValidationError {
    LayoutCorrupt,
    RecordCorrupt,
    LayoutMismatch,
    TypeMismatch,
    MetadataMismatch,
    MissingDeallocationMetadata,
    MissingAllocationRecord,
}

#[cold]
#[inline(never)]
fn panic_memory_tag_validation_error(error: MemoryTagValidationError) -> ! {
    match error {
        MemoryTagValidationError::LayoutCorrupt => {
            panic!("memory tag side-table layout corrupt")
        }
        MemoryTagValidationError::RecordCorrupt => panic!("memory tag record corrupt"),
        MemoryTagValidationError::LayoutMismatch => panic!("memory tag layout mismatch"),
        MemoryTagValidationError::TypeMismatch => panic!("memory tag type mismatch"),
        MemoryTagValidationError::MetadataMismatch => panic!("memory tag metadata mismatch"),
        MemoryTagValidationError::MissingDeallocationMetadata => {
            panic!("memory tag missing on deallocation metadata")
        }
        MemoryTagValidationError::MissingAllocationRecord => {
            panic!("memory tag missing allocation record")
        }
    }
}

#[inline]
fn memory_tag_record_layout(record: TaggedAllocation) -> Result<Layout, MemoryTagValidationError> {
    Layout::from_size_align(record.size, record.align)
        .map_err(|_| MemoryTagValidationError::LayoutCorrupt)
}

fn validate_memory_tagged_dealloc(
    record: TaggedAllocation,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Result<(), MemoryTagValidationError> {
    if record.size != layout.size() || record.align != layout.align() {
        return Err(MemoryTagValidationError::LayoutMismatch);
    }
    if metadata.has_type() && metadata.type_id != record.metadata.type_id {
        return Err(MemoryTagValidationError::TypeMismatch);
    }
    if metadata.has_type() && !memory_tag_typed_metadata_matches(record.metadata, metadata) {
        return Err(MemoryTagValidationError::MetadataMismatch);
    }
    Ok(())
}

unsafe fn try_verify_memory_tag_record(
    record: TaggedAllocation,
) -> Result<(), MemoryTagValidationError> {
    let layout = memory_tag_record_layout(record)?;
    if record.tag != derive_memory_tag(record.ptr, layout, record.metadata) {
        return Err(MemoryTagValidationError::RecordCorrupt);
    }
    Ok(())
}

unsafe fn verify_memory_tag_record(record: TaggedAllocation) {
    if let Err(error) = try_verify_memory_tag_record(record) {
        panic_memory_tag_validation_error(error);
    }
}

unsafe fn verify_memory_tagged_dealloc(
    record: TaggedAllocation,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Result<(), MemoryTagValidationError> {
    try_verify_memory_tag_record(record)?;
    validate_memory_tagged_dealloc(record, layout, metadata)
}

fn clear_global_memory_tagged_allocation(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
    requested: bool,
) -> bool {
    let mut table = global_memory_tag_table_for_ptr(ptr).lock();
    let slot = match find_global_memory_tag_slot(&mut *table, ptr) {
        Some(slot) => slot,
        None => return false,
    };
    if !requested {
        panic_memory_tag_validation_error(MemoryTagValidationError::MissingDeallocationMetadata);
    }
    let record = unsafe { *slot };
    if let Err(error) = unsafe { verify_memory_tagged_dealloc(record, layout, metadata) } {
        panic_memory_tag_validation_error(error);
    }
    unsafe {
        *slot = TaggedAllocation::empty();
    }
    clear_global_memory_tag_hot_slot(&mut *table, ptr);
    #[cfg(not(feature = "fixed_heap"))]
    unsafe {
        release_empty_memory_tag_overflow_pages(core::ptr::addr_of_mut!(table.overflow));
    }
    atomic_saturating_decrement(&GLOBAL_MEMORY_TAG_RECORD_COUNT);
    true
}

unsafe fn clear_memory_tagged_allocation(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) {
    let requested = metadata.requests(FLAG_MEMORY_TAGGING);
    if !requested
        && !current_thread_memory_tag_records_active()
        && !global_memory_tag_records_active()
    {
        return;
    }
    match find_memory_tag_slot(ptr) {
        Some(slot) => {
            if !requested {
                panic_memory_tag_validation_error(
                    MemoryTagValidationError::MissingDeallocationMetadata,
                );
            }
            let record = *slot;
            if let Err(error) = verify_memory_tagged_dealloc(record, layout, metadata) {
                panic_memory_tag_validation_error(error);
            }
            *slot = TaggedAllocation::empty();
            clear_memory_tag_hot_slot(ptr);
            #[cfg(not(feature = "fixed_heap"))]
            release_empty_memory_tag_overflow_pages(core::ptr::addr_of_mut!(MEMORY_TAG_OVERFLOW));
            MEMORY_TAG_RECORD_COUNT = MEMORY_TAG_RECORD_COUNT.saturating_sub(1);
            return;
        }
        None => {}
    }
    if clear_global_memory_tagged_allocation(ptr, layout, metadata, requested) {
        return;
    }
    if requested {
        panic_memory_tag_validation_error(MemoryTagValidationError::MissingAllocationRecord);
    }
}

#[inline]
fn round_up_to_page(size: usize) -> Option<usize> {
    let page = crate::PAGE_SIZE;
    size.checked_add(page - 1).map(|value| value & !(page - 1))
}

#[inline]
fn guard_page_eligible(layout: Layout, metadata: AllocationMetadata) -> bool {
    metadata.requests(FLAG_GUARD_PAGES)
        && layout.size() >= GUARD_PAGE_MIN_OBJECT_SIZE
        && layout.align() <= crate::PAGE_SIZE
        && round_up_to_page(layout.size()).is_some()
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn alloc_guarded(layout: Layout, metadata: AllocationMetadata) -> *mut u8 {
    if !guard_page_eligible(layout, metadata) {
        return core::ptr::null_mut();
    }
    let payload_size = match round_up_to_page(layout.size()) {
        Some(size) => size,
        None => return core::ptr::null_mut(),
    };
    let guard_size = crate::PAGE_SIZE;
    let total_size = match payload_size.checked_add(guard_size.saturating_mul(2)) {
        Some(size) => size,
        None => return core::ptr::null_mut(),
    };
    let rw = system_alloc::prots::get_prot(true, true, false);
    let none = system_alloc::prots::get_prot(false, false, false);
    let base = system_alloc::mmap(total_size, rw);
    if base.is_null() || base as usize == usize::MAX {
        return core::ptr::null_mut();
    }
    let high_guard = base.add(guard_size + payload_size);
    if !system_alloc::mprotect(base, guard_size, none)
        || !system_alloc::mprotect(high_guard, guard_size, none)
    {
        system_alloc::munmap(base, total_size);
        return core::ptr::null_mut();
    }
    base.add(guard_size)
}

#[cfg(feature = "fixed_heap")]
unsafe fn alloc_guarded(_layout: Layout, _metadata: AllocationMetadata) -> *mut u8 {
    core::ptr::null_mut()
}

unsafe fn dealloc_guarded(ptr: *mut u8, layout: Layout, metadata: AllocationMetadata) -> bool {
    if ptr.is_null() || !guard_page_eligible(layout, metadata) {
        return false;
    }
    #[cfg(not(feature = "fixed_heap"))]
    {
        let payload_size = match round_up_to_page(layout.size()) {
            Some(size) => size,
            None => return false,
        };
        let guard_size = crate::PAGE_SIZE;
        let total_size = match payload_size.checked_add(guard_size.saturating_mul(2)) {
            Some(size) => size,
            None => return false,
        };
        system_alloc::munmap(ptr.sub(guard_size), total_size);
        true
    }
    #[cfg(feature = "fixed_heap")]
    {
        false
    }
}

unsafe fn enqueue_delayed_free(
    alloc: &RustAllocator,
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) -> Option<DelayedFreeSlot> {
    if ptr.is_null() {
        return None;
    }

    let incoming = DelayedFreeSlot {
        ptr,
        size: layout.size(),
        align: layout.align(),
        auth: metadata_record_auth(ptr, layout, metadata),
        metadata,
    };
    let incoming_retained_bytes = delayed_free_retained_bytes_for_layout(layout);
    if incoming_retained_bytes > MAX_DELAYED_FREE_RETAINED_BYTES {
        record_stats_delayed_free_flush();
        return Some(incoming);
    }

    // The retained-byte budget is stricter than the slot budget: one thread can
    // fill the ring with smaller delayed frees, then free a larger object that
    // should still be quarantined for type-isolation reuse.  Evict enough older
    // retained bytes, largest-first to minimize churn, before falling back to an
    // immediate release of the incoming object.  Evicted records have the
    // delayed-free flag stripped in `release_delayed_slot`, so this cannot
    // recurse into the quarantine.
    while DELAYED_FREE_RETAINED_BYTES.saturating_add(incoming_retained_bytes)
        > MAX_DELAYED_FREE_RETAINED_BYTES
    {
        let evict_idx = match delayed_free_largest_occupied_index() {
            Some(idx) => idx,
            None => {
                record_stats_delayed_free_flush();
                return Some(incoming);
            }
        };
        let evicted = delayed_free_take_slot(evict_idx);
        record_stats_delayed_free_flush();
        release_delayed_slot(alloc, evicted);
    }

    let idx = DELAYED_FREE_CURSOR & (DELAYED_FREE_SLOTS - 1);
    let evicted = DELAYED_FREE[idx];
    let evicted_size = if evicted.is_empty() {
        0
    } else {
        evicted.retained_bytes()
    };
    DELAYED_FREE[idx] = incoming;
    delayed_free_mark_slot_occupied(idx, true);
    DELAYED_FREE_RETAINED_BYTES = DELAYED_FREE_RETAINED_BYTES
        .saturating_sub(evicted_size)
        .saturating_add(incoming_retained_bytes);
    DELAYED_FREE_CURSOR = (idx + 1) & (DELAYED_FREE_SLOTS - 1);
    record_stats_delayed_free_enqueue();
    if evicted.is_empty() {
        None
    } else {
        record_stats_delayed_free_flush();
        Some(evicted)
    }
}

unsafe fn delayed_free_take_slot(idx: usize) -> DelayedFreeSlot {
    let slot = DELAYED_FREE[idx];
    DELAYED_FREE[idx] = DelayedFreeSlot::empty();
    delayed_free_mark_slot_occupied(idx, false);
    if !slot.is_empty() {
        DELAYED_FREE_RETAINED_BYTES =
            DELAYED_FREE_RETAINED_BYTES.saturating_sub(slot.retained_bytes());
    }
    slot
}

#[inline]
fn delayed_free_slot_bit(idx: usize) -> Option<usize> {
    if idx < DELAYED_FREE_SLOTS && idx < usize::BITS as usize {
        Some(1usize << idx)
    } else {
        None
    }
}

#[inline]
fn delayed_free_valid_occupied_mask() -> usize {
    let bits = usize::BITS as usize;
    if DELAYED_FREE_SLOTS == 0 {
        0
    } else if DELAYED_FREE_SLOTS >= bits {
        usize::MAX
    } else {
        (1usize << DELAYED_FREE_SLOTS) - 1
    }
}

#[inline]
unsafe fn delayed_free_mark_slot_occupied(idx: usize, occupied: bool) {
    if let Some(bit) = delayed_free_slot_bit(idx) {
        if occupied {
            DELAYED_FREE_OCCUPIED_MASK |= bit;
        } else {
            DELAYED_FREE_OCCUPIED_MASK &= !bit;
        }
    }
}

unsafe fn delayed_free_rebuild_occupied_mask_and_largest() -> Option<usize> {
    let mut rebuilt_mask = 0usize;
    let mut best_idx = None;
    let mut best_retained_bytes = 0usize;
    let mut idx = 0usize;
    while idx < DELAYED_FREE_SLOTS {
        let slot = DELAYED_FREE[idx];
        if !slot.is_empty() {
            if let Some(bit) = delayed_free_slot_bit(idx) {
                rebuilt_mask |= bit;
            }
            let retained_bytes = slot.retained_bytes();
            if best_idx.is_none() || retained_bytes > best_retained_bytes {
                best_idx = Some(idx);
                best_retained_bytes = retained_bytes;
            }
        }
        idx += 1;
    }
    DELAYED_FREE_OCCUPIED_MASK = rebuilt_mask;
    best_idx
}

unsafe fn delayed_free_largest_occupied_index() -> Option<usize> {
    let mut occupied = DELAYED_FREE_OCCUPIED_MASK & delayed_free_valid_occupied_mask();
    let mut best_idx = None;
    let mut best_retained_bytes = 0usize;
    let mut scanned_bytes = 0usize;
    while occupied != 0 {
        let idx = occupied.trailing_zeros() as usize;
        occupied &= occupied - 1;
        let slot = DELAYED_FREE[idx];
        if slot.is_empty() {
            delayed_free_mark_slot_occupied(idx, false);
            continue;
        }
        let retained_bytes = slot.retained_bytes();
        scanned_bytes = scanned_bytes.saturating_add(retained_bytes);
        if best_idx.is_none() || retained_bytes > best_retained_bytes {
            best_idx = Some(idx);
            best_retained_bytes = retained_bytes;
        }
    }
    if scanned_bytes != DELAYED_FREE_RETAINED_BYTES {
        delayed_free_rebuild_occupied_mask_and_largest()
    } else {
        best_idx
    }
}

unsafe fn release_delayed_slot(alloc: &RustAllocator, slot: DelayedFreeSlot) {
    if slot.is_empty() {
        return;
    }
    let layout = checked_side_table_layout(slot.size, slot.align, "delayed free");
    verify_metadata_record_auth(slot.ptr, layout, slot.metadata, slot.metadata, slot.auth);
    let metadata = slot
        .metadata
        .with_flags(slot.metadata.flags & !FLAG_DELAYED_FREE);
    if cache_semantic_free(alloc, slot.ptr, layout, metadata) {
        return;
    }
    if metadata.requests(FLAG_FORCE_INITIALIZE) {
        core::ptr::write_bytes(slot.ptr, 0, layout.size());
    }
    alloc.dealloc_raw(slot.ptr, layout);
}

#[inline]
fn atomic_saturating_sub(counter: &AtomicUsize, amount: usize) {
    let mut current = counter.load(Ordering::Relaxed);
    loop {
        let next = current.saturating_sub(amount);
        match counter.compare_exchange_weak(current, next, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return,
            Err(observed) => current = observed,
        }
    }
}

/// Release allocator-owned semantic TLS before the ordinary thread cache is destroyed.
///
/// This path deliberately performs no allocation: it detaches each TLS-owned
/// object or side mapping first, then releases it through raw allocator/system
/// primitives.  Process-global recovery/tag tables are not touched.  Compiler
/// or memory-tagged objects that may outlive their allocating thread must have
/// requested `PLACEMENT_HINT_CROSS_THREAD_RECOVERY`; their records live in
/// those process-global tables rather than the TLS state cleared here.
///
/// Returns the number of cached or quarantined allocator objects released.
///
/// # Safety
///
/// The caller must run this on the owning thread, after that thread has stopped
/// using semantic allocation scopes and before its `ThreadCache` is cleaned up.
/// It must not be re-entered concurrently on the same thread.
pub(crate) unsafe fn drain_current_thread_semantic_state(alloc: &RustAllocator) -> usize {
    let mut released = 0usize;

    // Quarantine entries must bypass `release_delayed_slot`: that normal path
    // may place the object back into a semantic cache, while thread exit needs
    // a terminal raw release.
    let mut idx = 0usize;
    while idx < DELAYED_FREE_SLOTS {
        let slot = DELAYED_FREE[idx];
        DELAYED_FREE[idx] = DelayedFreeSlot::empty();
        if !slot.is_empty() {
            if let Ok(layout) = Layout::from_size_align(slot.size, slot.align) {
                if !metadata_record_auth_is_valid(
                    slot.ptr,
                    layout,
                    slot.metadata,
                    slot.metadata,
                    slot.auth,
                ) {
                    idx += 1;
                    continue;
                }
                if slot.metadata.requests(FLAG_FORCE_INITIALIZE) {
                    core::ptr::write_bytes(slot.ptr, 0, layout.size());
                }
                alloc.dealloc_raw(slot.ptr, layout);
                released = released.saturating_add(1);
            }
        }
        idx += 1;
    }
    DELAYED_FREE_CURSOR = 0;
    DELAYED_FREE_RETAINED_BYTES = 0;
    DELAYED_FREE_OCCUPIED_MASK = 0;

    TYPE_CACHE_HOT_SLOT = TypeCacheHotSlot::empty();
    TYPE_CACHE_IDENTITY_HOT_SLOT = TypeCacheIdentityHotSlot::empty();
    let inline = INLINE_TYPE_CACHE_ENTRY;
    INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
    if !inline.is_empty() {
        if let Ok(layout) = Layout::from_size_align(inline.size, inline.align) {
            alloc.dealloc_raw(inline.ptr, layout);
            released = released.saturating_add(1);
        }
    }

    idx = 0;
    while idx < TYPE_CACHE_SLOTS {
        let slot = TYPE_CACHE[idx];
        TYPE_CACHE[idx] = TypeCacheSlot::empty();
        let mut current = slot.head;
        let mut remaining = core::cmp::min(slot.count, MAX_TYPE_CACHE_DEPTH);
        while remaining != 0 && !current.is_null() {
            if (current as usize) & (TYPE_CACHE_NODE_ALIGN - 1) != 0 {
                break;
            }
            let node = current as *mut usize;
            let next = node.read() as *mut u8;
            let size = node.add(1).read();
            let align = node.add(2).read();
            match Layout::from_size_align(size, align) {
                Ok(layout) => {
                    alloc.dealloc_raw(current, layout);
                    released = released.saturating_add(1);
                }
                Err(_) => {
                    break;
                }
            }
            current = next;
            remaining -= 1;
        }
        idx += 1;
    }

    SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket::empty();
    released = released.saturating_add(drain_inline_segregated_type_cache_at_thread_exit(
        alloc,
        SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
    ));
    #[cfg(not(feature = "fixed_heap"))]
    {
        released = released.saturating_add(drain_inline_segregated_type_cache_at_thread_exit(
            alloc,
            SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
        ));
    }

    idx = 0;
    while idx < TYPE_CACHE_SLOTS {
        let bucket = SEGREGATED_TYPE_CACHE[idx];
        SEGREGATED_TYPE_CACHE[idx] = SegregatedTypeCacheBucket::empty();
        released = released.saturating_add(drain_segregated_bucket_at_thread_exit(alloc, bucket));
        idx += 1;
    }
    SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
    SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
    SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;

    #[cfg(not(feature = "fixed_heap"))]
    {
        let cache_ptr = HUGEPAGE_SEGREGATED_TYPE_CACHE;
        let mapping_size = HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE;
        HUGEPAGE_SEGREGATED_TYPE_CACHE = core::ptr::null_mut();
        HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE = 0;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING = system_alloc::HugePageMmapBacking::Unmapped;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
        HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
        if !cache_ptr.is_null() {
            idx = 0;
            while idx < TYPE_CACHE_SLOTS {
                let bucket = (*cache_ptr)[idx];
                (*cache_ptr)[idx] = SegregatedTypeCacheBucket::empty();
                released =
                    released.saturating_add(drain_segregated_bucket_at_thread_exit(alloc, bucket));
                idx += 1;
            }
            let size = if mapping_size == 0 {
                size_of::<[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]>()
            } else {
                mapping_size
            };
            system_alloc::munmap(cache_ptr as *mut u8, size);
        }
    }

    // These are metadata records for caller-owned live objects, not allocator
    // cache ownership.  Local records are invalid once their owning thread
    // exits; cross-thread-capable records are process-global and remain intact.
    MEMORY_TAGS = [TaggedAllocation::empty(); MEMORY_TAG_FAST_SLOTS];
    MEMORY_TAG_HOT_SLOT = MemoryTagHotSlot::empty();
    MEMORY_TAG_RECORD_COUNT = 0;
    #[cfg(not(feature = "fixed_heap"))]
    {
        let mut page = MEMORY_TAG_OVERFLOW;
        MEMORY_TAG_OVERFLOW = core::ptr::null_mut();
        while !page.is_null() {
            let next = (*page).next;
            system_alloc::munmap(page as *mut u8, size_of::<MemoryTagPage>());
            page = next;
        }
    }

    let mut local_recovery_records: usize = if FAST_AUTO_ALLOCATION_RECORD_INLINE.is_empty() {
        0
    } else {
        1
    };
    idx = 0;
    while idx < FAST_AUTO_ALLOCATION_RECORD_SLOTS {
        let record = FAST_AUTO_ALLOCATION_RECORDS[idx];
        if !record.is_available() {
            local_recovery_records = local_recovery_records.saturating_add(1);
        }
        idx += 1;
    }
    FAST_AUTO_ALLOCATION_RECORD_INLINE = AutoAllocationRecord::empty();
    FAST_AUTO_ALLOCATION_RECORDS =
        [AutoAllocationRecord::empty(); FAST_AUTO_ALLOCATION_RECORD_SLOTS];
    FAST_AUTO_ALLOCATION_RECORD_COUNT = 0;
    FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT = FastAutoAllocationRecordHotSlot::empty();
    atomic_saturating_sub(
        &FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT,
        local_recovery_records,
    );

    clear_auto_layout_metadata_hot_slot();
    AUTO_COMPILER_TYPE_IDS_TLS_CURSOR = 0;
    AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH = 0;
    ACTIVE_METADATA = AllocationMetadata::unknown();
    SEMANTIC_SCOPE_STACK = [AllocationMetadata::unknown(); SEMANTIC_SCOPE_STACK_CAPACITY];
    SEMANTIC_SCOPE_STACK_DEPTH = 0;
    SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH = 0;
    SEMANTIC_SCOPE_OVERFLOW_STACK =
        [AllocationMetadata::unknown(); SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY];
    SEMANTIC_SCOPE_OVERFLOW_BOUNDARY_ACTIVE = AllocationMetadata::unknown();
    SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS = false;

    released
}

unsafe fn drain_inline_segregated_type_cache_at_thread_exit(
    alloc: &RustAllocator,
    cache_domain: u8,
) -> usize {
    let mut released = 0usize;
    let mut index = 0usize;
    while index < inline_segregated_type_cache_capacity(cache_domain) {
        let slot = inline_segregated_type_cache_entry_at(cache_domain, index);
        let entry = *slot;
        *slot = SegregatedTypeCacheEntry::empty();
        released =
            released.saturating_add(drain_segregated_entry_at_thread_exit(alloc, entry) as usize);
        index += 1;
    }
    released
}

unsafe fn drain_segregated_bucket_at_thread_exit(
    alloc: &RustAllocator,
    bucket: SegregatedTypeCacheBucket,
) -> usize {
    let mut released = 0usize;
    let mut idx = 0usize;
    let count = core::cmp::min(bucket.count, SEGREGATED_TYPE_CACHE_DEPTH);
    while idx < count {
        released = released
            .saturating_add(
                drain_segregated_entry_at_thread_exit(alloc, bucket.entries[idx]) as usize,
            );
        idx += 1;
    }
    released
}

unsafe fn drain_segregated_entry_at_thread_exit(
    alloc: &RustAllocator,
    entry: SegregatedTypeCacheEntry,
) -> bool {
    if entry.is_empty() {
        return false;
    }
    let layout = match Layout::from_size_align(entry.size, entry.align) {
        Ok(layout) => layout,
        Err(_) => return false,
    };
    if !metadata_record_auth_is_valid(
        entry.ptr,
        layout,
        entry.metadata,
        entry.metadata,
        entry.auth,
    ) {
        return false;
    }
    if entry.metadata.requests(FLAG_FORCE_INITIALIZE) {
        core::ptr::write_bytes(entry.ptr, 0, layout.size());
    }
    alloc.dealloc_raw(entry.ptr, layout);
    true
}

const SEMANTIC_REALLOC_IN_PLACE_INCOMPATIBLE_FLAGS: u32 =
    FLAG_DELAYED_FREE | FLAG_GUARD_PAGES | FLAG_MEMORY_TAGGING;

#[inline]
fn semantic_realloc_same_size_class(old_layout: Layout, new_size: usize) -> bool {
    old_layout.size() != 0
        && new_size != 0
        && crate::size_class::get_size_class(old_layout.size()).index()
            == crate::size_class::get_size_class(new_size).index()
}

#[inline]
fn semantic_realloc_same_storage_semantics(
    old_metadata: AllocationMetadata,
    new_metadata: AllocationMetadata,
) -> bool {
    let same_semantic_identity = old_metadata.type_id == new_metadata.type_id
        || (old_metadata.is_layout_derived() && new_metadata.is_layout_derived());

    same_semantic_identity
        && old_metadata.module_id == new_metadata.module_id
        && old_metadata.flags == new_metadata.flags
        && old_metadata.lifetime_hint == new_metadata.lifetime_hint
        && old_metadata.placement_hint == new_metadata.placement_hint
}

#[inline]
pub(crate) fn semantic_realloc_can_reuse_in_place(
    old_layout: Layout,
    new_size: usize,
    old_metadata: AllocationMetadata,
    new_metadata: AllocationMetadata,
) -> bool {
    semantic_realloc_same_storage_semantics(old_metadata, new_metadata)
        && ((old_metadata.flags | new_metadata.flags)
            & SEMANTIC_REALLOC_IN_PLACE_INCOMPATIBLE_FLAGS)
            == 0
        && semantic_realloc_same_size_class(old_layout, new_size)
}

#[inline]
fn semantic_zero_size_ptr(layout: Layout) -> *mut u8 {
    layout.align() as *mut u8
}

#[inline]
fn semantic_realloc_old_pointer_supported(ptr: *mut u8, old_layout: Layout) -> bool {
    old_layout.size() == 0 || !ptr.is_null()
}

/// Semantic allocation API used by compiler-instrumented and manual callers.
pub unsafe trait SemanticAlloc {
    unsafe fn alloc_with_metadata(&self, layout: Layout, metadata: AllocationMetadata) -> *mut u8;
    unsafe fn dealloc_with_metadata(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    );

    unsafe fn realloc_with_metadata(
        &self,
        ptr: *mut u8,
        old_layout: Layout,
        new_size: usize,
        metadata: AllocationMetadata,
    ) -> *mut u8 {
        self.realloc_with_split_metadata(ptr, old_layout, new_size, metadata, metadata)
    }

    unsafe fn realloc_with_split_metadata(
        &self,
        ptr: *mut u8,
        old_layout: Layout,
        new_size: usize,
        old_metadata: AllocationMetadata,
        new_metadata: AllocationMetadata,
    ) -> *mut u8 {
        let new_layout = match Layout::from_size_align(new_size, old_layout.align()) {
            Ok(layout) => layout,
            Err(_) => return core::ptr::null_mut(),
        };
        if !semantic_realloc_old_pointer_supported(ptr, old_layout) {
            return core::ptr::null_mut();
        }
        if new_size == 0 {
            if !ptr.is_null() && old_layout.size() != 0 {
                self.dealloc_with_metadata(ptr, old_layout, old_metadata);
            }
            return semantic_zero_size_ptr(new_layout);
        }
        let new_ptr = self.alloc_with_metadata(new_layout, new_metadata);
        if !new_ptr.is_null() && !ptr.is_null() {
            core::ptr::copy_nonoverlapping(
                ptr,
                new_ptr,
                core::cmp::min(old_layout.size(), new_size),
            );
            self.dealloc_with_metadata(ptr, old_layout, old_metadata);
        }
        new_ptr
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RecoveryRecordPolicy {
    /// Preserve the ordinary SemanticAlloc behavior: only create a recovery
    /// record while a conservative compiler/runtime bridge has requested one.
    ScopedIfEnabled,
    /// Conservative ABI path: always create a recovery record for later raw
    /// GlobalAlloc deallocation or reallocation.
    Force,
}

impl RecoveryRecordPolicy {
    #[inline]
    fn should_record(self) -> bool {
        match self {
            RecoveryRecordPolicy::ScopedIfEnabled => auto_allocation_recovery_recording_enabled(),
            RecoveryRecordPolicy::Force => true,
        }
    }
}

impl RustAllocator {
    #[inline]
    unsafe fn record_fast_recovery_or_global(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> bool {
        record_recovery_auto_allocation_metadata(ptr, layout, metadata)
    }

    #[inline]
    unsafe fn alloc_with_compiler_type_metadata_fast(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
        record_recovery: bool,
    ) -> *mut u8 {
        if layout.size() == 0 {
            return semantic_zero_size_ptr(layout);
        }
        let ptr = if let Some(ptr) = pop_compiler_type_metadata_cache(layout, metadata) {
            ptr
        } else {
            self.alloc_raw(layout)
        };
        if record_recovery && !self.record_fast_recovery_or_global(ptr, layout, metadata) {
            self.dealloc_raw(ptr, layout);
            return core::ptr::null_mut();
        }
        record_stats_alloc_layout(metadata, layout);
        ptr
    }

    #[inline]
    unsafe fn dealloc_with_compiler_type_metadata_fast(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
        recover_allocation_record: bool,
    ) {
        if ptr.is_null() || layout.size() == 0 {
            return;
        }
        let cache_metadata = if recover_allocation_record {
            match recover_auto_allocation_record_metadata(ptr, layout, true) {
                Some(recorded_metadata) => {
                    deallocation_metadata_after_recovery_record(metadata, recorded_metadata)
                }
                None => metadata,
            }
        } else {
            metadata
        };
        record_stats_dealloc_layout(cache_metadata, layout);
        if cache_compiler_type_metadata_free(self, ptr, layout, cache_metadata) {
            return;
        }
        self.dealloc_raw(ptr, layout);
    }

    #[inline]
    unsafe fn release_unfinished_semantic_allocation(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) {
        let _ = recover_auto_allocation_record_metadata(ptr, layout, true);
        if dealloc_guarded(ptr, layout, metadata) {
            return;
        }
        self.dealloc_raw(ptr, layout);
    }

    #[inline]
    unsafe fn release_unfinished_semantic_allocation_no_recovery(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) {
        if dealloc_guarded(ptr, layout, metadata) {
            return;
        }
        self.dealloc_raw(ptr, layout);
    }

    /// Finish an allocation on the exact-metadata ABI path.
    ///
    /// Keep this separate from `finish_semantic_allocation_with_policy` rather
    /// than adding a disabled recovery mode to the shared helper.  On arm64e no_std builds,
    /// merely reaching Rust Mach-O TLV
    /// bookkeeping for recovery records can fault before allocator logic runs.
    /// The local ABI already requires the matching deallocation/reallocation to
    /// carry exact metadata, so recovery side tables are not part of this
    /// contract.
    #[inline]
    unsafe fn finish_semantic_allocation_no_recovery(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> *mut u8 {
        if ptr.is_null() {
            return ptr;
        }
        if metadata.requests(FLAG_FORCE_INITIALIZE) {
            core::ptr::write_bytes(ptr, 0, layout.size());
        }
        if record_memory_tagged_allocation(ptr, layout, metadata).is_err() {
            self.release_unfinished_semantic_allocation_no_recovery(ptr, layout, metadata);
            return core::ptr::null_mut();
        }
        record_stats_alloc_layout(metadata, layout);
        ptr
    }

    #[inline]
    unsafe fn finish_semantic_allocation_with_policy(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
        recovery_policy: RecoveryRecordPolicy,
    ) -> *mut u8 {
        if ptr.is_null() {
            return ptr;
        }
        if metadata.requests(FLAG_FORCE_INITIALIZE) {
            core::ptr::write_bytes(ptr, 0, layout.size());
        }
        let recovery_recorded = match recovery_policy {
            RecoveryRecordPolicy::ScopedIfEnabled => {
                if auto_allocation_recovery_recording_enabled() {
                    record_auto_allocation_metadata(ptr, layout, metadata)
                } else {
                    true
                }
            }
            RecoveryRecordPolicy::Force => {
                record_recovery_auto_allocation_metadata(ptr, layout, metadata)
            }
        };
        if !recovery_recorded {
            self.release_unfinished_semantic_allocation(ptr, layout, metadata);
            return core::ptr::null_mut();
        }
        if record_memory_tagged_allocation(ptr, layout, metadata).is_err() {
            self.release_unfinished_semantic_allocation(ptr, layout, metadata);
            return core::ptr::null_mut();
        }
        record_stats_alloc_layout(metadata, layout);
        ptr
    }

    #[inline]
    unsafe fn finish_semantic_allocation_with_recovery(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
        force_recovery_record: bool,
    ) -> *mut u8 {
        let policy = if force_recovery_record {
            RecoveryRecordPolicy::Force
        } else {
            RecoveryRecordPolicy::ScopedIfEnabled
        };
        self.finish_semantic_allocation_with_policy(ptr, layout, metadata, policy)
    }

    #[inline]
    unsafe fn alloc_with_metadata_inner_policy(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
        recovery_policy: RecoveryRecordPolicy,
    ) -> *mut u8 {
        if layout.size() == 0 {
            return semantic_zero_size_ptr(layout);
        }
        if layout_derived_raw_only_fast_path(metadata) {
            return self.alloc_raw(layout);
        }
        if compiler_type_isolated_recovery_fast_path(metadata) {
            return self.alloc_with_compiler_type_metadata_fast(
                layout,
                metadata,
                recovery_policy.should_record(),
            );
        }
        if guard_page_eligible(layout, metadata) {
            let ptr = alloc_guarded(layout, metadata);
            return self.finish_semantic_allocation_with_policy(
                ptr,
                layout,
                metadata,
                recovery_policy,
            );
        }

        if let Some(ptr) = pop_semantic_type_cache(layout, metadata) {
            return self.finish_semantic_allocation_with_policy(
                ptr,
                layout,
                metadata,
                recovery_policy,
            );
        }

        let ptr = self.alloc_raw(layout);
        self.finish_semantic_allocation_with_policy(ptr, layout, metadata, recovery_policy)
    }

    #[inline]
    unsafe fn alloc_with_metadata_inner_no_recovery(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> *mut u8 {
        if layout.size() == 0 {
            return semantic_zero_size_ptr(layout);
        }
        if layout_derived_raw_only_fast_path(metadata) {
            return self.alloc_raw(layout);
        }
        if compiler_type_isolated_recovery_fast_path(metadata) {
            return self.alloc_with_compiler_type_metadata_fast(layout, metadata, false);
        }
        if guard_page_eligible(layout, metadata) {
            let ptr = alloc_guarded(layout, metadata);
            return self.finish_semantic_allocation_no_recovery(ptr, layout, metadata);
        }

        if let Some(ptr) = pop_semantic_type_cache(layout, metadata) {
            return self.finish_semantic_allocation_no_recovery(ptr, layout, metadata);
        }

        let ptr = self.alloc_raw(layout);
        self.finish_semantic_allocation_no_recovery(ptr, layout, metadata)
    }

    #[inline]
    unsafe fn alloc_with_metadata_inner(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
        force_recovery_record: bool,
    ) -> *mut u8 {
        let policy = if force_recovery_record {
            RecoveryRecordPolicy::Force
        } else {
            RecoveryRecordPolicy::ScopedIfEnabled
        };
        self.alloc_with_metadata_inner_policy(layout, metadata, policy)
    }

    #[inline]
    pub(crate) unsafe fn alloc_with_no_recovery_metadata(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> *mut u8 {
        self.alloc_with_metadata_inner_no_recovery(layout, metadata)
    }

    #[inline]
    pub(crate) unsafe fn alloc_with_recovery_metadata(
        &self,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> *mut u8 {
        if layout.size() == 0 {
            return semantic_zero_size_ptr(layout);
        }
        if compiler_type_isolated_recovery_fast_path(metadata) {
            return self.alloc_with_compiler_type_metadata_fast(layout, metadata, true);
        }
        self.alloc_with_metadata_inner(layout, metadata, true)
    }

    #[inline]
    unsafe fn dealloc_with_metadata_inner(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
        recover_allocation_record: bool,
    ) {
        if ptr.is_null() || layout.size() == 0 {
            return;
        }
        if layout_derived_raw_only_fast_path(metadata) {
            self.dealloc_raw(ptr, layout);
            return;
        }
        if compiler_type_isolated_recovery_fast_path(metadata) {
            return self.dealloc_with_compiler_type_metadata_fast(
                ptr,
                layout,
                metadata,
                recover_allocation_record,
            );
        }
        let dealloc_metadata = if recover_allocation_record {
            match recover_auto_allocation_record_metadata(ptr, layout, true) {
                Some(recorded_metadata) => {
                    deallocation_metadata_after_recovery_record(metadata, recorded_metadata)
                }
                None => metadata,
            }
        } else {
            metadata
        };
        clear_memory_tagged_allocation(ptr, layout, dealloc_metadata);
        record_stats_dealloc_layout(dealloc_metadata, layout);
        if dealloc_guarded(ptr, layout, dealloc_metadata) {
            return;
        }
        if dealloc_metadata.requests(FLAG_DELAYED_FREE) {
            if let Some(slot) = enqueue_delayed_free(self, ptr, layout, dealloc_metadata) {
                release_delayed_slot(self, slot);
            }
            return;
        }
        if cache_semantic_free(self, ptr, layout, dealloc_metadata) {
            return;
        }
        if !ptr.is_null() && dealloc_metadata.requests(FLAG_FORCE_INITIALIZE) {
            core::ptr::write_bytes(ptr, 0, layout.size());
        }
        self.dealloc_raw(ptr, layout);
    }

    #[inline]
    pub(crate) unsafe fn dealloc_with_recovered_metadata(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) {
        if ptr.is_null() || layout.size() == 0 {
            return;
        }
        if compiler_type_isolated_recovery_fast_path(metadata) {
            return self.dealloc_with_compiler_type_metadata_fast(ptr, layout, metadata, false);
        }
        self.dealloc_with_metadata_inner(ptr, layout, metadata, false);
    }
}

unsafe impl SemanticAlloc for RustAllocator {
    #[inline]
    unsafe fn alloc_with_metadata(&self, layout: Layout, metadata: AllocationMetadata) -> *mut u8 {
        self.alloc_with_metadata_inner(layout, metadata, false)
    }

    #[inline]
    unsafe fn dealloc_with_metadata(
        &self,
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) {
        self.dealloc_with_metadata_inner(ptr, layout, metadata, true);
    }

    #[inline]
    unsafe fn realloc_with_split_metadata(
        &self,
        ptr: *mut u8,
        old_layout: Layout,
        new_size: usize,
        old_metadata: AllocationMetadata,
        new_metadata: AllocationMetadata,
    ) -> *mut u8 {
        let new_layout = match Layout::from_size_align(new_size, old_layout.align()) {
            Ok(layout) => layout,
            Err(_) => return core::ptr::null_mut(),
        };
        if !semantic_realloc_old_pointer_supported(ptr, old_layout) {
            return core::ptr::null_mut();
        }
        if new_size == 0 {
            if !ptr.is_null() && old_layout.size() != 0 {
                self.dealloc_with_metadata(ptr, old_layout, old_metadata);
            }
            return semantic_zero_size_ptr(new_layout);
        }
        if !ptr.is_null()
            && semantic_realloc_can_reuse_in_place(old_layout, new_size, old_metadata, new_metadata)
        {
            if auto_allocation_recovery_recording_enabled()
                && !replace_auto_allocation_record_for_reallocation(
                    ptr,
                    old_layout,
                    new_layout,
                    new_metadata,
                )
            {
                return core::ptr::null_mut();
            }
            record_stats_alloc_layout(new_metadata, new_layout);
            if old_metadata != new_metadata {
                record_stats_dealloc_layout(old_metadata, old_layout);
            }
            if new_size > old_layout.size() && new_metadata.requests(FLAG_FORCE_INITIALIZE) {
                core::ptr::write_bytes(ptr.add(old_layout.size()), 0, new_size - old_layout.size());
            }
            return ptr;
        }

        let new_ptr = self.alloc_with_metadata(new_layout, new_metadata);
        if !new_ptr.is_null() && !ptr.is_null() {
            core::ptr::copy_nonoverlapping(
                ptr,
                new_ptr,
                core::cmp::min(old_layout.size(), new_size),
            );
            self.dealloc_with_metadata(ptr, old_layout, old_metadata);
        }
        new_ptr
    }
}

/// Safe-ish convenience wrapper for callers that need a non-null slice result.
pub unsafe fn allocate_semantic_slice<A: SemanticAlloc>(
    alloc: &A,
    layout: Layout,
    metadata: AllocationMetadata,
) -> core::result::Result<NonNull<[u8]>, core::alloc::AllocError> {
    let ptr = alloc.alloc_with_metadata(layout, metadata);
    let ptr = NonNull::new(ptr).ok_or(core::alloc::AllocError)?;
    Ok(NonNull::slice_from_raw_parts(ptr, layout.size()))
}

pub fn semantic_stats_snapshot() -> SemanticStatsSnapshot {
    SEMANTIC_STATS.snapshot()
}

pub fn semantic_fallback_attribution_snapshot() -> SemanticFallbackAttributionSnapshot {
    SEMANTIC_FALLBACK_ATTRIBUTION.snapshot()
}

pub fn semantic_metadata_validation_snapshot() -> SemanticMetadataValidationSnapshot {
    SEMANTIC_METADATA_VALIDATION.snapshot()
}

pub fn semantic_scope_depth_snapshot() -> SemanticScopeDepthSnapshot {
    unsafe {
        let represented_overflow_depth =
            if SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH > SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
                SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY
            } else {
                SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH
            };
        SemanticScopeDepthSnapshot {
            main_depth: SEMANTIC_SCOPE_STACK_DEPTH,
            overflow_depth: SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH,
            overflow_capacity: SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY,
            represented_depth: SEMANTIC_SCOPE_STACK_DEPTH + represented_overflow_depth,
            active_overflow_scope_represented: SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH != 0
                && SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH <= SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY,
        }
    }
}

#[inline]
fn semantic_type_stats_slot(metadata: AllocationMetadata) -> usize {
    let (shard_idx, slot_idx) = semantic_type_stats_shard_and_slot(metadata);
    shard_idx * SEMANTIC_TYPE_STATS_SHARD_SLOTS + slot_idx
}

#[inline]
fn semantic_type_stats_shard_and_slot(metadata: AllocationMetadata) -> (usize, usize) {
    let hash = fnv1a_mix(FNV1A_OFFSET, b"semantic-type-stats-slot-v1");
    let hash = fnv1a_mix(hash, &metadata.type_id.to_le_bytes());
    let hash = fnv1a_mix(hash, &metadata.module_id.to_le_bytes());
    let hash = fnv1a_mix(hash, &metadata.callsite.to_le_bytes());
    let mixed = (hash as usize).wrapping_mul(0x517c_c1b7usize);
    (
        mixed & (SEMANTIC_TYPE_STATS_SHARD_COUNT - 1),
        (mixed >> 3) & (SEMANTIC_TYPE_STATS_SHARD_SLOTS - 1),
    )
}

#[inline]
fn semantic_type_stats_shard(metadata: AllocationMetadata) -> usize {
    semantic_type_stats_shard_and_slot(metadata).0
}

#[inline]
fn semantic_type_stats_shard_bit(shard_idx: usize) -> usize {
    1usize << shard_idx
}

#[inline]
fn semantic_type_stats_mark_shard_active(shard_idx: usize) {
    SEMANTIC_TYPE_STATS_ACTIVE_SHARDS
        .fetch_or(semantic_type_stats_shard_bit(shard_idx), Ordering::Release);
}

#[inline]
fn semantic_type_stats_shard_active(active_mask: usize, shard_idx: usize) -> bool {
    active_mask & semantic_type_stats_shard_bit(shard_idx) != 0
}

fn with_semantic_type_stats_entry<R>(
    metadata: AllocationMetadata,
    f: impl FnOnce(&mut SemanticTypeStatsSlot) -> R,
) -> Option<R> {
    if !metadata.has_type() {
        return None;
    }
    let (home_shard_idx, home_start) = semantic_type_stats_shard_and_slot(metadata);
    let active_mask = SEMANTIC_TYPE_STATS_ACTIVE_SHARDS.load(Ordering::Acquire);
    let mut first_inactive: Option<(usize, usize)> = None;
    let mut shard_offset = 0;
    while shard_offset < SEMANTIC_TYPE_STATS_SHARD_COUNT {
        let shard_idx = (home_shard_idx + shard_offset) & (SEMANTIC_TYPE_STATS_SHARD_COUNT - 1);
        let start = if shard_offset == 0 { home_start } else { 0 };
        if shard_offset != 0 && !semantic_type_stats_shard_active(active_mask, shard_idx) {
            if first_inactive.is_none() {
                first_inactive = Some((shard_idx, start));
            }
            shard_offset += 1;
            continue;
        }
        let mut shard = SEMANTIC_TYPE_STATS[shard_idx].lock();
        let mut offset = 0;
        while offset < SEMANTIC_TYPE_STATS_SHARD_SLOTS {
            let idx = (start + offset) & (SEMANTIC_TYPE_STATS_SHARD_SLOTS - 1);
            let slot = &mut shard.slots[idx];
            if slot.is_empty() {
                semantic_type_stats_mark_shard_active(shard_idx);
                slot.type_id = metadata.type_id;
                slot.module_id = metadata.module_id;
                slot.callsite = metadata.callsite;
                return Some(f(slot));
            }
            if semantic_type_stats_slot_matches(*slot, metadata) {
                if slot.module_id == UNKNOWN_SEMANTIC_ID {
                    slot.module_id = metadata.module_id;
                }
                if slot.callsite == 0 {
                    slot.callsite = metadata.callsite;
                }
                return Some(f(slot));
            }
            offset += 1;
        }
        shard_offset += 1;
    }
    if let Some((shard_idx, start)) = first_inactive {
        let mut shard = SEMANTIC_TYPE_STATS[shard_idx].lock();
        let mut offset = 0;
        while offset < SEMANTIC_TYPE_STATS_SHARD_SLOTS {
            let idx = (start + offset) & (SEMANTIC_TYPE_STATS_SHARD_SLOTS - 1);
            let slot = &mut shard.slots[idx];
            if slot.is_empty() {
                semantic_type_stats_mark_shard_active(shard_idx);
                slot.type_id = metadata.type_id;
                slot.module_id = metadata.module_id;
                slot.callsite = metadata.callsite;
                return Some(f(slot));
            }
            if semantic_type_stats_slot_matches(*slot, metadata) {
                if slot.module_id == UNKNOWN_SEMANTIC_ID {
                    slot.module_id = metadata.module_id;
                }
                if slot.callsite == 0 {
                    slot.callsite = metadata.callsite;
                }
                return Some(f(slot));
            }
            offset += 1;
        }
    }
    SEMANTIC_STATS.record_semantic_type_stats_drop();
    None
}

#[inline]
fn semantic_type_stats_slot_matches(
    slot: SemanticTypeStatsSlot,
    metadata: AllocationMetadata,
) -> bool {
    if slot.type_id != metadata.type_id {
        return false;
    }

    let module_matches = metadata.module_id == UNKNOWN_SEMANTIC_ID
        || slot.module_id == UNKNOWN_SEMANTIC_ID
        || slot.module_id == metadata.module_id;
    let callsite_matches =
        metadata.callsite == 0 || slot.callsite == 0 || slot.callsite == metadata.callsite;
    module_matches && callsite_matches
}

unsafe fn record_type_stats_alloc(
    metadata: AllocationMetadata,
    bytes: usize,
    observed_size: usize,
    observed_align: usize,
) {
    with_semantic_type_stats_entry(metadata, |slot| {
        if slot.module_id == UNKNOWN_SEMANTIC_ID {
            slot.module_id = metadata.module_id;
        }
        if slot.callsite == 0 {
            slot.callsite = metadata.callsite;
        }
        slot.allocations = slot.allocations.saturating_add(1);
        slot.allocated_bytes = slot.allocated_bytes.saturating_add(bytes);
        if observed_size != 0 {
            slot.observed_alloc_size = observed_size;
        }
        if observed_align != 0 {
            slot.observed_alloc_align = observed_align;
        }
        slot.policy_flags_seen |= metadata.flags;
    });
}

unsafe fn record_type_stats_dealloc(
    metadata: AllocationMetadata,
    observed_size: usize,
    observed_align: usize,
) {
    with_semantic_type_stats_entry(metadata, |slot| {
        slot.deallocations = slot.deallocations.saturating_add(1);
        if observed_size != 0 {
            slot.observed_dealloc_size = observed_size;
        }
        if observed_align != 0 {
            slot.observed_dealloc_align = observed_align;
        }
        slot.policy_flags_seen |= metadata.flags;
    });
}

unsafe fn record_type_stats_cache_hit(metadata: AllocationMetadata) {
    with_semantic_type_stats_entry(metadata, |slot| {
        slot.cache_hits = slot.cache_hits.saturating_add(1);
        slot.policy_flags_seen |= metadata.flags;
    });
}

unsafe fn record_type_stats_cache_insert(metadata: AllocationMetadata) {
    with_semantic_type_stats_entry(metadata, |slot| {
        slot.cache_inserts = slot.cache_inserts.saturating_add(1);
        slot.policy_flags_seen |= metadata.flags;
    });
}

unsafe fn record_type_stats_cache_bypass(metadata: AllocationMetadata) {
    with_semantic_type_stats_entry(metadata, |slot| {
        slot.cache_bypasses = slot.cache_bypasses.saturating_add(1);
        slot.policy_flags_seen |= metadata.flags;
    });
}

pub fn semantic_type_stats_snapshot(out: &mut [SemanticTypeStatsSnapshot]) -> usize {
    let mut written = 0;
    for shard_lock in SEMANTIC_TYPE_STATS.iter() {
        let shard = shard_lock.lock();
        let mut idx = 0;
        while idx < SEMANTIC_TYPE_STATS_SHARD_SLOTS {
            let slot = shard.slots[idx];
            if !slot.is_empty() {
                if written < out.len() {
                    out[written] = slot.snapshot();
                }
                written += 1;
            }
            idx += 1;
        }
    }
    written
}

pub fn semantic_type_stats_reset() {
    for shard_lock in SEMANTIC_TYPE_STATS.iter() {
        shard_lock.lock().slots = [SemanticTypeStatsSlot::empty(); SEMANTIC_TYPE_STATS_SHARD_SLOTS];
    }
    SEMANTIC_TYPE_STATS_ACTIVE_SHARDS.store(0, Ordering::Release);
    SEMANTIC_STATS.reset_semantic_type_stats_drops();
}

pub fn semantic_stats_reset() {
    SEMANTIC_STATS.reset();
    SEMANTIC_FALLBACK_ATTRIBUTION.reset();
    SEMANTIC_METADATA_VALIDATION.reset();
    semantic_type_stats_reset();
    semantic_stats_recording_enable();
}

/// Return true when fallback `GlobalAlloc` calls should pay semantic coverage
/// accounting costs.
///
/// Paper-performance runs that do not explicitly collect semantic coverage
/// should stay on the allocator fast path. Coverage harnesses call
/// `semantic_stats_reset()` before the measured interval, which flips this
/// gate on so untyped fallback allocations still contribute to the honest
/// denominator.
pub fn semantic_stats_recording_enabled() -> bool {
    semantic_stats_recording_flags() & SLOW_PATH_STATS != 0
}

/// Enable semantic coverage counters for the current process.
pub fn semantic_stats_recording_enable() {
    if semantic_stats_feature_enabled() {
        semantic_slow_path_set(SLOW_PATH_STATS | SLOW_PATH_TYPE_STATS);
    }
}

#[inline]
pub(crate) fn semantic_fallback_attribution_record_raw_alloc_no_metadata(bytes: usize) {
    SEMANTIC_FALLBACK_ATTRIBUTION.record_raw_alloc_no_metadata(bytes);
}

#[inline]
pub(crate) fn semantic_fallback_attribution_record_raw_dealloc_no_metadata() {
    SEMANTIC_FALLBACK_ATTRIBUTION.record_raw_dealloc_no_metadata();
}

#[inline]
pub(crate) fn semantic_fallback_attribution_record_raw_realloc_no_metadata(bytes: usize) {
    SEMANTIC_FALLBACK_ATTRIBUTION.record_raw_realloc_no_metadata(bytes);
}

#[inline]
pub(crate) fn semantic_fallback_attribution_record_raw_realloc_moved_dealloc_no_metadata() {
    SEMANTIC_FALLBACK_ATTRIBUTION.record_raw_realloc_moved_dealloc_no_metadata();
}

#[inline]
pub(crate) fn semantic_fallback_attribution_record_realloc_recorded_old_metadata_new_allocation(
    bytes: usize,
) {
    SEMANTIC_FALLBACK_ATTRIBUTION.record_realloc_recorded_old_metadata_new_allocation(bytes);
}

/// Disable semantic coverage counters for ordinary fallback allocations.
pub fn semantic_stats_recording_disable() {
    semantic_slow_path_clear(SLOW_PATH_STATS | SLOW_PATH_TYPE_STATS);
}

/// Return true when per-type semantic coverage rows are collected.
///
/// Aggregate counters are enough for lightweight performance probes, while
/// C002 coverage evidence and compiler-runtime companion probes need per-type
/// rows to correlate runtime events with compiler-emitted type mappings.
pub fn semantic_type_stats_recording_enabled() -> bool {
    semantic_stats_recording_flags() & SLOW_PATH_TYPE_STATS != 0
}

/// Enable per-type semantic coverage rows without changing aggregate counters.
pub fn semantic_type_stats_recording_enable() {
    if semantic_stats_feature_enabled() {
        semantic_slow_path_set(SLOW_PATH_TYPE_STATS);
    }
}

/// Disable per-type semantic coverage rows while preserving aggregate counters.
pub fn semantic_type_stats_recording_disable() {
    semantic_slow_path_clear(SLOW_PATH_TYPE_STATS);
}

#[inline]
fn record_stats_alloc(metadata: AllocationMetadata, bytes: usize) {
    record_stats_alloc_observed(metadata, bytes, bytes, 0);
}

#[inline]
fn record_stats_alloc_layout(metadata: AllocationMetadata, layout: Layout) {
    record_stats_alloc_observed(metadata, layout.size(), layout.size(), layout.align());
}

#[inline]
fn record_stats_alloc_observed(
    metadata: AllocationMetadata,
    bytes: usize,
    observed_size: usize,
    observed_align: usize,
) {
    let flags = semantic_stats_recording_flags();
    if flags & SLOW_PATH_STATS != 0 {
        SEMANTIC_STATS.record_alloc(metadata, bytes);
    }
    if flags & SLOW_PATH_TYPE_STATS != 0 {
        unsafe {
            record_type_stats_alloc(metadata, bytes, observed_size, observed_align);
        }
    }
}

#[inline]
fn record_stats_dealloc(metadata: AllocationMetadata) {
    record_stats_dealloc_observed(metadata, 0, 0);
}

#[inline]
fn record_stats_dealloc_layout(metadata: AllocationMetadata, layout: Layout) {
    record_stats_dealloc_observed(metadata, layout.size(), layout.align());
}

#[inline]
fn record_stats_dealloc_observed(
    metadata: AllocationMetadata,
    observed_size: usize,
    observed_align: usize,
) {
    let flags = semantic_stats_recording_flags();
    if flags & SLOW_PATH_STATS != 0 {
        SEMANTIC_STATS.record_dealloc(metadata);
    }
    if flags & SLOW_PATH_TYPE_STATS != 0 {
        unsafe {
            record_type_stats_dealloc(metadata, observed_size, observed_align);
        }
    }
}

#[inline]
fn record_stats_type_cache_hit(metadata: AllocationMetadata) {
    let flags = semantic_stats_recording_flags();
    if flags & SLOW_PATH_STATS != 0 {
        SEMANTIC_STATS.record_type_cache_hit();
    }
    if flags & SLOW_PATH_TYPE_STATS != 0 {
        unsafe {
            record_type_stats_cache_hit(metadata);
        }
    }
}

#[inline]
fn record_stats_type_cache_insert(metadata: AllocationMetadata) {
    let flags = semantic_stats_recording_flags();
    if flags & SLOW_PATH_STATS != 0 {
        SEMANTIC_STATS.record_type_cache_insert();
    }
    if flags & SLOW_PATH_TYPE_STATS != 0 {
        unsafe {
            record_type_stats_cache_insert(metadata);
        }
    }
}

#[inline]
fn record_stats_type_cache_bypass(metadata: AllocationMetadata) {
    let flags = semantic_stats_recording_flags();
    if flags & SLOW_PATH_STATS != 0 {
        SEMANTIC_STATS.record_type_cache_bypass();
    }
    if flags & SLOW_PATH_TYPE_STATS != 0 {
        unsafe {
            record_type_stats_cache_bypass(metadata);
        }
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn record_stats_metadata_pac_auth_sign() {
    if semantic_stats_recording_enabled() {
        SEMANTIC_STATS.record_metadata_pac_auth_sign();
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn record_stats_metadata_pac_auth_verification(valid: bool) {
    if semantic_stats_recording_enabled() {
        SEMANTIC_STATS.record_metadata_pac_auth_verification(valid);
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn record_stats_metadata_pac_software_fallback_sign() {
    if semantic_stats_recording_enabled() {
        SEMANTIC_STATS.record_metadata_pac_software_fallback_sign();
    }
}

#[cfg(all(feature = "pac", target_arch = "aarch64"))]
#[inline]
fn record_stats_metadata_pac_software_fallback_verification(valid: bool) {
    if semantic_stats_recording_enabled() {
        SEMANTIC_STATS.record_metadata_pac_software_fallback_verification(valid);
    }
}

#[inline]
fn record_stats_delayed_free_enqueue() {
    if semantic_stats_recording_enabled() {
        SEMANTIC_STATS.record_delayed_free_enqueue();
    }
}

#[inline]
fn record_stats_delayed_free_flush() {
    if semantic_stats_recording_enabled() {
        SEMANTIC_STATS.record_delayed_free_flush();
    }
}

#[inline]
fn ffi_metadata(type_id: u64, module_id: u64, flags: u32, callsite: u64) -> AllocationMetadata {
    ffi_metadata_with_hints(type_id, module_id, flags, 0, 0, callsite)
}

#[inline]
fn ffi_metadata_with_hints(
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> AllocationMetadata {
    AllocationMetadata {
        type_id,
        module_id,
        flags,
        lifetime_hint,
        placement_hint,
        callsite,
    }
}

#[inline]
fn local_scope_metadata(metadata: AllocationMetadata) -> AllocationMetadata {
    metadata.with_placement_hint(metadata.placement_hint | PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY)
}

#[inline]
unsafe fn alloc_layout_with_ffi_metadata(layout: Layout, metadata: AllocationMetadata) -> *mut u8 {
    let allocator = RustAllocator::new();
    with_auto_allocation_recovery_recording(|| allocator.alloc_with_metadata(layout, metadata))
}

/// Allocate through the metadata ABI without installing an allocation recovery
/// record.
///
/// This is the fast ABI for compiler lowerings that also rewrite the matching
/// deallocation/reallocation sites to carry exact metadata.  The existing
/// recovery-recording ABI remains the conservative default for mixed
/// instrumentation where a later ordinary `GlobalAlloc::dealloc` may need to
/// recover the allocation-site identity.
#[inline]
unsafe fn alloc_layout_with_ffi_metadata_local(
    layout: Layout,
    metadata: AllocationMetadata,
) -> *mut u8 {
    let allocator = RustAllocator::new();
    allocator.alloc_with_no_recovery_metadata(layout, metadata)
}

#[inline]
unsafe fn alloc_zeroed_layout_with_ffi_metadata(
    layout: Layout,
    metadata: AllocationMetadata,
) -> *mut u8 {
    let ptr = alloc_layout_with_ffi_metadata(layout, metadata);
    if !ptr.is_null() && layout.size() != 0 {
        core::ptr::write_bytes(ptr, 0, layout.size());
    }
    ptr
}

#[inline]
unsafe fn alloc_zeroed_layout_with_ffi_metadata_local(
    layout: Layout,
    metadata: AllocationMetadata,
) -> *mut u8 {
    let ptr = alloc_layout_with_ffi_metadata_local(layout, metadata);
    if !ptr.is_null() && layout.size() != 0 {
        core::ptr::write_bytes(ptr, 0, layout.size());
    }
    ptr
}

#[inline]
fn recovered_or_requested_reallocation_old_metadata(
    ptr: *mut u8,
    old_layout: Layout,
    requested_metadata: AllocationMetadata,
) -> AllocationMetadata {
    recorded_reallocation_old_metadata(ptr, old_layout).unwrap_or(requested_metadata)
}

#[inline]
fn take_or_requested_reallocation_old_metadata(
    ptr: *mut u8,
    old_layout: Layout,
    requested_metadata: AllocationMetadata,
) -> AllocationMetadata {
    take_auto_allocation_records_for_reallocation(ptr, old_layout).unwrap_or(requested_metadata)
}

#[inline]
unsafe fn realloc_layout_with_ffi_metadata(
    ptr: *mut u8,
    old_layout: Layout,
    new_size: usize,
    metadata: AllocationMetadata,
) -> *mut u8 {
    if !semantic_realloc_old_pointer_supported(ptr, old_layout) {
        return core::ptr::null_mut();
    }
    let allocator = RustAllocator::new();
    let old_metadata = recovered_or_requested_reallocation_old_metadata(ptr, old_layout, metadata);
    with_auto_allocation_recovery_recording(|| {
        allocator.realloc_with_split_metadata(ptr, old_layout, new_size, old_metadata, metadata)
    })
}

/// Reallocate through the metadata ABI without installing a recovery record for
/// the new allocation.
///
/// Any existing old-pointer recovery record is consumed first so mixed
/// conservative-to-local transitions do not leave stale `(ptr, old_layout)`
/// metadata behind.  The caller/compiler must still lower the later
/// deallocation/reallocation with exact metadata because ordinary
/// `GlobalAlloc::dealloc` will not be able to recover the new allocation's
/// metadata from a side table.
#[inline]
unsafe fn realloc_layout_with_ffi_metadata_local(
    ptr: *mut u8,
    old_layout: Layout,
    new_size: usize,
    metadata: AllocationMetadata,
) -> *mut u8 {
    if !semantic_realloc_old_pointer_supported(ptr, old_layout) {
        return core::ptr::null_mut();
    }
    let allocator = RustAllocator::new();
    let old_metadata = take_or_requested_reallocation_old_metadata(ptr, old_layout, metadata);
    allocator.realloc_with_split_metadata(ptr, old_layout, new_size, old_metadata, metadata)
}

#[inline]
unsafe fn dealloc_layout_with_ffi_metadata(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) {
    let allocator = RustAllocator::new();
    allocator.dealloc_with_metadata(ptr, layout, metadata);
}

/// Deallocate through exact compiler-supplied metadata without consulting the
/// recovery side table.
///
/// Use this only for compiler-paired local metadata ABI paths where the matching
/// allocation/reallocation was also lowered to a no-recovery local ABI.  Mixed
/// conservative paths must use `dealloc_layout_with_ffi_metadata` so stale or
/// panic/unwind recovery records are consumed correctly.
#[inline]
unsafe fn dealloc_layout_with_ffi_metadata_local(
    ptr: *mut u8,
    layout: Layout,
    metadata: AllocationMetadata,
) {
    let allocator = RustAllocator::new();
    allocator.dealloc_with_recovered_metadata(ptr, layout, metadata);
}

/// Compiler/runtime instrumentation ABI for semantic allocation.
///
/// Returns null on invalid layouts or allocation failure.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_alloc_with_metadata(
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(size, align) {
        Ok(layout) => alloc_layout_with_ffi_metadata(
            layout,
            ffi_metadata(type_id, module_id, flags, callsite),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Fast compiler/runtime instrumentation ABI for semantic allocation.
///
/// Unlike `__unialloc_alloc_with_metadata`, this does not create a recovery
/// side-table record.  Use it only when the compiler/runtime also emits exact
/// metadata for the matching deallocation/reallocation path.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_alloc_with_metadata_local(
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(size, align) {
        Ok(layout) => alloc_layout_with_ffi_metadata_local(
            layout,
            ffi_metadata(type_id, module_id, flags, callsite),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Compiler/runtime instrumentation ABI for semantic allocation with full
/// metadata hints.
///
/// This is the metadata-complete companion to
/// `__unialloc_alloc_with_metadata`: rustc-driver/MIR instrumentation can pass
/// lifetime and placement policy classes instead of forcing the allocator to
/// treat those fields as unknown/zero.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_alloc_with_metadata_hints(
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(size, align) {
        Ok(layout) => alloc_layout_with_ffi_metadata(
            layout,
            ffi_metadata_with_hints(
                type_id,
                module_id,
                flags,
                lifetime_hint,
                placement_hint,
                callsite,
            ),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Fast metadata-complete compiler/runtime allocation ABI.
///
/// This is the no-recovery companion to
/// `__unialloc_alloc_with_metadata_hints`; it is intended for compiler passes
/// that lower both allocation and deallocation/reallocation sites with exact
/// lifetime/placement metadata.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_alloc_with_metadata_hints_local(
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(size, align) {
        Ok(layout) => alloc_layout_with_ffi_metadata_local(
            layout,
            ffi_metadata_with_hints(
                type_id,
                module_id,
                flags,
                lifetime_hint,
                placement_hint,
                callsite,
            ),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Rust ABI companion for MIR replacement of `std::alloc::alloc(Layout)`.
///
/// Unlike the C ABI above this keeps the original MIR `Layout` operand intact,
/// so the compiler pass does not have to synthesize field reads or additional
/// `Layout::size/align` calls around the allocator terminator.
pub unsafe fn __unialloc_alloc_layout_with_metadata(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    alloc_layout_with_ffi_metadata(layout, ffi_metadata(type_id, module_id, flags, callsite))
}

/// Fast Rust ABI companion for MIR replacement of `std::alloc::alloc(Layout)`.
///
/// The compiler may select this variant when it can also lower the matching
/// deallocation/reallocation path to carry exact metadata, avoiding recovery
/// side-table traffic on the hot allocation path.
pub unsafe fn __unialloc_alloc_layout_with_metadata_local(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    alloc_layout_with_ffi_metadata_local(layout, ffi_metadata(type_id, module_id, flags, callsite))
}

/// Rust ABI companion for MIR replacement of `std::alloc::alloc(Layout)` with
/// full metadata hints.
pub unsafe fn __unialloc_alloc_layout_with_metadata_hints(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    alloc_layout_with_ffi_metadata(
        layout,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    )
}

/// Fast Rust ABI companion for MIR replacement of `std::alloc::alloc(Layout)`
/// with full metadata hints.
pub unsafe fn __unialloc_alloc_layout_with_metadata_hints_local(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    alloc_layout_with_ffi_metadata_local(
        layout,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    )
}

/// Rust ABI companion for MIR replacement of
/// `std::alloc::alloc_zeroed(Layout)`.
pub unsafe fn __unialloc_alloc_zeroed_layout_with_metadata(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    alloc_zeroed_layout_with_ffi_metadata(layout, ffi_metadata(type_id, module_id, flags, callsite))
}

/// Fast Rust ABI companion for MIR replacement of
/// `std::alloc::alloc_zeroed(Layout)`.
pub unsafe fn __unialloc_alloc_zeroed_layout_with_metadata_local(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    alloc_zeroed_layout_with_ffi_metadata_local(
        layout,
        ffi_metadata(type_id, module_id, flags, callsite),
    )
}

/// Rust ABI companion for MIR replacement of
/// `std::alloc::alloc_zeroed(Layout)` with full metadata hints.
pub unsafe fn __unialloc_alloc_zeroed_layout_with_metadata_hints(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    alloc_zeroed_layout_with_ffi_metadata(
        layout,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    )
}

/// Fast Rust ABI companion for MIR replacement of
/// `std::alloc::alloc_zeroed(Layout)` with full metadata hints.
pub unsafe fn __unialloc_alloc_zeroed_layout_with_metadata_hints_local(
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    alloc_zeroed_layout_with_ffi_metadata_local(
        layout,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    )
}

/// Compiler/runtime instrumentation ABI for semantic deallocation.
///
/// Returns false when the layout is invalid.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_dealloc_with_metadata(
    ptr: *mut u8,
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> bool {
    match Layout::from_size_align(size, align) {
        Ok(layout) => {
            dealloc_layout_with_ffi_metadata(
                ptr,
                layout,
                ffi_metadata(type_id, module_id, flags, callsite),
            );
            true
        }
        Err(_) => false,
    }
}

/// Fast compiler/runtime instrumentation ABI for semantic deallocation.
///
/// Unlike `__unialloc_dealloc_with_metadata`, this does not consult or consume
/// the recovery side table.  Use it only when the compiler/runtime selected the
/// local metadata ABI for the matching allocation/reallocation site.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_dealloc_with_metadata_local(
    ptr: *mut u8,
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> bool {
    match Layout::from_size_align(size, align) {
        Ok(layout) => {
            dealloc_layout_with_ffi_metadata_local(
                ptr,
                layout,
                ffi_metadata(type_id, module_id, flags, callsite),
            );
            true
        }
        Err(_) => false,
    }
}

/// Compiler/runtime instrumentation ABI for semantic deallocation with full
/// metadata hints.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_dealloc_with_metadata_hints(
    ptr: *mut u8,
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> bool {
    match Layout::from_size_align(size, align) {
        Ok(layout) => {
            dealloc_layout_with_ffi_metadata(
                ptr,
                layout,
                ffi_metadata_with_hints(
                    type_id,
                    module_id,
                    flags,
                    lifetime_hint,
                    placement_hint,
                    callsite,
                ),
            );
            true
        }
        Err(_) => false,
    }
}

/// Fast compiler/runtime instrumentation ABI for semantic deallocation with
/// full metadata hints.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_dealloc_with_metadata_hints_local(
    ptr: *mut u8,
    size: usize,
    align: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> bool {
    match Layout::from_size_align(size, align) {
        Ok(layout) => {
            dealloc_layout_with_ffi_metadata_local(
                ptr,
                layout,
                ffi_metadata_with_hints(
                    type_id,
                    module_id,
                    flags,
                    lifetime_hint,
                    placement_hint,
                    callsite,
                ),
            );
            true
        }
        Err(_) => false,
    }
}

/// Rust ABI companion for MIR replacement of `std::alloc::dealloc(ptr, Layout)`.
///
/// It returns unit to match the original `dealloc` call terminator.
pub unsafe fn __unialloc_dealloc_layout_with_metadata(
    ptr: *mut u8,
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) {
    dealloc_layout_with_ffi_metadata(
        ptr,
        layout,
        ffi_metadata(type_id, module_id, flags, callsite),
    );
}

/// Fast Rust ABI companion for MIR replacement of
/// `std::alloc::dealloc(ptr, Layout)`.
///
/// It returns unit to match the original `dealloc` call terminator and avoids
/// recovery side-table lookup on compiler-paired local metadata paths.
pub unsafe fn __unialloc_dealloc_layout_with_metadata_local(
    ptr: *mut u8,
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) {
    dealloc_layout_with_ffi_metadata_local(
        ptr,
        layout,
        ffi_metadata(type_id, module_id, flags, callsite),
    );
}

/// Rust ABI companion for MIR replacement of `std::alloc::dealloc(ptr, Layout)`
/// with full metadata hints.
pub unsafe fn __unialloc_dealloc_layout_with_metadata_hints(
    ptr: *mut u8,
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) {
    dealloc_layout_with_ffi_metadata(
        ptr,
        layout,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    );
}

/// Fast Rust ABI companion for MIR replacement of
/// `std::alloc::dealloc(ptr, Layout)` with full metadata hints.
pub unsafe fn __unialloc_dealloc_layout_with_metadata_hints_local(
    ptr: *mut u8,
    layout: Layout,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) {
    dealloc_layout_with_ffi_metadata_local(
        ptr,
        layout,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    );
}

/// Compiler/runtime instrumentation ABI for semantic reallocation.
///
/// Returns null on invalid layouts or allocation failure.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_realloc_with_metadata(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(old_size, old_align) {
        Ok(old_layout) => realloc_layout_with_ffi_metadata(
            ptr,
            old_layout,
            new_size,
            ffi_metadata(type_id, module_id, flags, callsite),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Fast compiler/runtime instrumentation ABI for semantic reallocation.
///
/// This consumes any old recovery record but does not create a recovery record
/// for the new allocation.  Use it only when the compiler/runtime also emits
/// exact metadata for the later deallocation/reallocation path.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_realloc_with_metadata_local(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(old_size, old_align) {
        Ok(old_layout) => realloc_layout_with_ffi_metadata_local(
            ptr,
            old_layout,
            new_size,
            ffi_metadata(type_id, module_id, flags, callsite),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Compiler/runtime instrumentation ABI for semantic reallocation with full
/// metadata hints.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_realloc_with_metadata_hints(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(old_size, old_align) {
        Ok(old_layout) => realloc_layout_with_ffi_metadata(
            ptr,
            old_layout,
            new_size,
            ffi_metadata_with_hints(
                type_id,
                module_id,
                flags,
                lifetime_hint,
                placement_hint,
                callsite,
            ),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Fast compiler/runtime instrumentation ABI for semantic reallocation with
/// full metadata hints.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_realloc_with_metadata_hints_local(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(old_size, old_align) {
        Ok(old_layout) => realloc_layout_with_ffi_metadata_local(
            ptr,
            old_layout,
            new_size,
            ffi_metadata_with_hints(
                type_id,
                module_id,
                flags,
                lifetime_hint,
                placement_hint,
                callsite,
            ),
        ),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Rust ABI companion for MIR replacement of
/// `std::alloc::realloc(ptr, Layout, new_size)`.
pub unsafe fn __unialloc_realloc_layout_with_metadata(
    ptr: *mut u8,
    old_layout: Layout,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    realloc_layout_with_ffi_metadata(
        ptr,
        old_layout,
        new_size,
        ffi_metadata(type_id, module_id, flags, callsite),
    )
}

/// Fast Rust ABI companion for MIR replacement of
/// `std::alloc::realloc(ptr, Layout, new_size)`.
///
/// This variant is for compiler passes that lower paired allocation,
/// reallocation, and deallocation sites with exact metadata, so no new recovery
/// side-table record is required.
pub unsafe fn __unialloc_realloc_layout_with_metadata_local(
    ptr: *mut u8,
    old_layout: Layout,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> *mut u8 {
    realloc_layout_with_ffi_metadata_local(
        ptr,
        old_layout,
        new_size,
        ffi_metadata(type_id, module_id, flags, callsite),
    )
}

/// Rust ABI companion for MIR replacement of
/// `std::alloc::realloc(ptr, Layout, new_size)` with full metadata hints.
pub unsafe fn __unialloc_realloc_layout_with_metadata_hints(
    ptr: *mut u8,
    old_layout: Layout,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    realloc_layout_with_ffi_metadata(
        ptr,
        old_layout,
        new_size,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    )
}

/// Fast Rust ABI companion for MIR replacement of
/// `std::alloc::realloc(ptr, Layout, new_size)` with full metadata hints.
pub unsafe fn __unialloc_realloc_layout_with_metadata_hints_local(
    ptr: *mut u8,
    old_layout: Layout,
    new_size: usize,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> *mut u8 {
    realloc_layout_with_ffi_metadata_local(
        ptr,
        old_layout,
        new_size,
        ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ),
    )
}

/// Compiler/runtime instrumentation ABI for semantic reallocation when the
/// old object and new allocation site have distinct metadata.
///
/// Returns null on invalid layouts or allocation failure.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_realloc_with_split_metadata(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
    old_type_id: u64,
    old_module_id: u64,
    old_flags: u32,
    old_callsite: u64,
    new_type_id: u64,
    new_module_id: u64,
    new_flags: u32,
    new_callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(old_size, old_align) {
        Ok(old_layout) => {
            let allocator = RustAllocator::new();
            with_auto_allocation_recovery_recording(|| {
                allocator.realloc_with_split_metadata(
                    ptr,
                    old_layout,
                    new_size,
                    ffi_metadata(old_type_id, old_module_id, old_flags, old_callsite),
                    ffi_metadata(new_type_id, new_module_id, new_flags, new_callsite),
                )
            })
        }
        Err(_) => core::ptr::null_mut(),
    }
}

/// Compiler/runtime instrumentation ABI for semantic reallocation when the old
/// and new sites have distinct full metadata.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_realloc_with_split_metadata_hints(
    ptr: *mut u8,
    old_size: usize,
    old_align: usize,
    new_size: usize,
    old_type_id: u64,
    old_module_id: u64,
    old_flags: u32,
    old_lifetime_hint: u16,
    old_placement_hint: u16,
    old_callsite: u64,
    new_type_id: u64,
    new_module_id: u64,
    new_flags: u32,
    new_lifetime_hint: u16,
    new_placement_hint: u16,
    new_callsite: u64,
) -> *mut u8 {
    match Layout::from_size_align(old_size, old_align) {
        Ok(old_layout) => {
            let allocator = RustAllocator::new();
            with_auto_allocation_recovery_recording(|| {
                allocator.realloc_with_split_metadata(
                    ptr,
                    old_layout,
                    new_size,
                    ffi_metadata_with_hints(
                        old_type_id,
                        old_module_id,
                        old_flags,
                        old_lifetime_hint,
                        old_placement_hint,
                        old_callsite,
                    ),
                    ffi_metadata_with_hints(
                        new_type_id,
                        new_module_id,
                        new_flags,
                        new_lifetime_hint,
                        new_placement_hint,
                        new_callsite,
                    ),
                )
            })
        }
        Err(_) => core::ptr::null_mut(),
    }
}

/// Copy the current semantic statistics snapshot into `out`.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_stats_snapshot(
    out: *mut SemanticStatsSnapshot,
) -> bool {
    if out.is_null() {
        return false;
    }
    out.write(semantic_stats_snapshot());
    true
}

/// Return the semantic-stats snapshot ABI version exported by this runtime.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_stats_snapshot_abi_version() -> u32 {
    SEMANTIC_STATS_SNAPSHOT_ABI_VERSION
}

/// Return the number of bytes required for `SemanticStatsSnapshot`.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_stats_snapshot_size() -> usize {
    size_of::<SemanticStatsSnapshot>()
}

/// Copy the current semantic statistics snapshot only when the caller-provided
/// buffer is large enough for this runtime's `SemanticStatsSnapshot`.
///
/// This is the safe FFI entrypoint for external compiler/runtime integrations
/// that may be built against a different snapshot layout.  The legacy
/// `__unialloc_semantic_stats_snapshot` remains available for in-tree callers
/// that compile against the exact Rust definition.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_stats_snapshot_checked(
    out: *mut SemanticStatsSnapshot,
    out_size: usize,
) -> bool {
    if out.is_null() || out_size < size_of::<SemanticStatsSnapshot>() {
        return false;
    }
    out.write(semantic_stats_snapshot());
    true
}

/// Copy the current semantic fallback-attribution snapshot into `out`.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_fallback_attribution_snapshot(
    out: *mut SemanticFallbackAttributionSnapshot,
) -> bool {
    if out.is_null() {
        return false;
    }
    out.write(semantic_fallback_attribution_snapshot());
    true
}

/// Return the fallback-attribution snapshot ABI version exported by this runtime.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_fallback_attribution_snapshot_abi_version() -> u32 {
    SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION
}

/// Return the number of bytes required for `SemanticFallbackAttributionSnapshot`.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_fallback_attribution_snapshot_size() -> usize {
    size_of::<SemanticFallbackAttributionSnapshot>()
}

/// Copy the current fallback-attribution snapshot only when the caller-provided
/// buffer is large enough for this runtime's `SemanticFallbackAttributionSnapshot`.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_fallback_attribution_snapshot_checked(
    out: *mut SemanticFallbackAttributionSnapshot,
    out_size: usize,
) -> bool {
    if out.is_null() || out_size < size_of::<SemanticFallbackAttributionSnapshot>() {
        return false;
    }
    out.write(semantic_fallback_attribution_snapshot());
    true
}

/// Copy the current metadata-validation snapshot into `out`.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_metadata_validation_snapshot(
    out: *mut SemanticMetadataValidationSnapshot,
) -> bool {
    if out.is_null() {
        return false;
    }
    out.write(semantic_metadata_validation_snapshot());
    true
}

/// Return the metadata-validation snapshot ABI version exported by this runtime.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_metadata_validation_snapshot_abi_version() -> u32 {
    SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION
}

/// Return the number of bytes required for `SemanticMetadataValidationSnapshot`.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_metadata_validation_snapshot_size() -> usize {
    size_of::<SemanticMetadataValidationSnapshot>()
}

/// Copy the current metadata-validation snapshot only when the caller-provided
/// buffer is large enough for this runtime's `SemanticMetadataValidationSnapshot`.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_metadata_validation_snapshot_checked(
    out: *mut SemanticMetadataValidationSnapshot,
    out_size: usize,
) -> bool {
    if out.is_null() || out_size < size_of::<SemanticMetadataValidationSnapshot>() {
        return false;
    }
    out.write(semantic_metadata_validation_snapshot());
    true
}

/// Copy the current thread-local semantic-scope stack depth into `out`.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_scope_depth_snapshot(
    out: *mut SemanticScopeDepthSnapshot,
) -> bool {
    if out.is_null() {
        return false;
    }
    out.write(semantic_scope_depth_snapshot());
    true
}

/// Return the per-type semantic-stats row ABI version exported by this runtime.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_type_stats_snapshot_abi_version() -> u32 {
    SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION
}

/// Return the number of bytes required for one `SemanticTypeStatsSnapshot` row.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_type_stats_snapshot_record_size() -> usize {
    size_of::<SemanticTypeStatsSnapshot>()
}

/// Copy per-type semantic statistics only when the caller-provided row layout
/// exactly matches this runtime's `SemanticTypeStatsSnapshot`.
///
/// Returns the number of populated rows, matching
/// `__unialloc_semantic_type_stats_snapshot`, or zero if the ABI check fails.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_type_stats_snapshot_checked(
    out: *mut SemanticTypeStatsSnapshot,
    len: usize,
    record_size: usize,
) -> usize {
    if record_size != size_of::<SemanticTypeStatsSnapshot>() {
        return 0;
    }
    __unialloc_semantic_type_stats_snapshot(out, len)
}

/// Copy per-type semantic statistics for the current thread into `out`.
///
/// Returns the number of populated type-stat rows, even when `len` is smaller
/// than the populated table. Passing a null pointer with `len == 0` can be used
/// to query the row count without copying records.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_type_stats_snapshot(
    out: *mut SemanticTypeStatsSnapshot,
    len: usize,
) -> usize {
    if out.is_null() {
        return if len == 0 {
            let mut scratch: [SemanticTypeStatsSnapshot; 0] = [];
            semantic_type_stats_snapshot(&mut scratch)
        } else {
            0
        };
    }
    let out_slice = core::slice::from_raw_parts_mut(out, len);
    semantic_type_stats_snapshot(out_slice)
}

/// Reset semantic statistics counters for a fresh evaluation interval.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_stats_reset() {
    semantic_stats_reset();
}

/// Enable process-wide layout-derived metadata for evaluation-only
/// unmodified-`GlobalAlloc` benchmark runs.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_auto_metadata_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> bool {
    semantic_auto_metadata_enable(module_id, flags, callsite);
    true
}

/// Enable process-wide compiler-id replay auto metadata for evaluation
/// harnesses that run ordinary `GlobalAlloc` benchmark targets.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_auto_compiler_metadata_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
) -> bool {
    semantic_auto_compiler_metadata_enable(module_id, flags, callsite, type_ids, len)
}

/// Enable process-wide finite compiler-id stream auto metadata for evaluation
/// harnesses that run ordinary `GlobalAlloc` benchmark targets.
#[no_mangle]
pub unsafe extern "C" fn __unialloc_semantic_auto_compiler_metadata_stream_enable(
    module_id: u64,
    flags: u32,
    callsite: u64,
    type_ids: *const u64,
    len: usize,
) -> bool {
    semantic_auto_compiler_metadata_stream_enable(module_id, flags, callsite, type_ids, len)
}

/// Disable process-wide layout-derived auto metadata.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_auto_metadata_disable() {
    semantic_auto_metadata_disable();
}

/// Enter a thread-local semantic metadata scope for ordinary `GlobalAlloc`
/// calls. The returned value must be passed to
/// `__unialloc_semantic_scope_exit` to restore the previous scope.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_enter(
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) -> AllocationMetadata {
    unsafe { set_active_metadata(ffi_metadata(type_id, module_id, flags, callsite)) }
}

/// Enter a thread-local semantic metadata scope with full metadata hints.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_enter_hints(
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) -> AllocationMetadata {
    unsafe {
        set_active_metadata(ffi_metadata_with_hints(
            type_id,
            module_id,
            flags,
            lifetime_hint,
            placement_hint,
            callsite,
        ))
    }
}

/// Restore the previous metadata scope returned by
/// `__unialloc_semantic_scope_enter`.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_exit(previous: AllocationMetadata) {
    unsafe {
        restore_active_metadata(previous);
    }
}

#[inline]
unsafe fn push_overflow_semantic_scope_metadata(metadata: AllocationMetadata) {
    let overflow_depth = SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH;
    if overflow_depth < SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
        SEMANTIC_SCOPE_OVERFLOW_STACK[overflow_depth] = ACTIVE_METADATA;
        set_semantic_scope_active_metadata(metadata);
    } else if overflow_depth == SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
        SEMANTIC_SCOPE_OVERFLOW_BOUNDARY_ACTIVE = ACTIVE_METADATA;
        set_semantic_scope_active_metadata(AllocationMetadata::unknown());
    }
    SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH = overflow_depth.saturating_add(1);
}

#[inline]
unsafe fn pop_overflow_semantic_scope_metadata() -> bool {
    let overflow_depth = SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH;
    if overflow_depth == 0 {
        return false;
    }

    let next_depth = overflow_depth - 1;
    if overflow_depth > SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
        if next_depth == SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
            set_semantic_scope_active_metadata(SEMANTIC_SCOPE_OVERFLOW_BOUNDARY_ACTIVE);
            SEMANTIC_SCOPE_OVERFLOW_BOUNDARY_ACTIVE = AllocationMetadata::unknown();
        }
    } else if next_depth < SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
        set_semantic_scope_active_metadata(SEMANTIC_SCOPE_OVERFLOW_STACK[next_depth]);
        SEMANTIC_SCOPE_OVERFLOW_STACK[next_depth] = AllocationMetadata::unknown();
    }
    SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH = next_depth;
    true
}

#[inline(always)]
fn push_semantic_scope_metadata(metadata: AllocationMetadata) {
    unsafe {
        let depth = SEMANTIC_SCOPE_STACK_DEPTH;
        if depth < SEMANTIC_SCOPE_STACK_CAPACITY {
            if depth == 0 {
                // The compiler-inserted scope hot path nearly always starts
                // from the unknown/default scope. Avoid writing a full
                // AllocationMetadata stack slot just to restore the same
                // unknown value on pop. If a manual/source scope is already
                // active, keep the exact previous metadata for correctness.
                let previous = ACTIVE_METADATA;
                if scoped_metadata_is_active(previous) {
                    SEMANTIC_SCOPE_STACK[0] = previous;
                    SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS = true;
                } else {
                    SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS = false;
                }
            } else {
                SEMANTIC_SCOPE_STACK[depth] = ACTIVE_METADATA;
            }
            SEMANTIC_SCOPE_STACK_DEPTH = depth + 1;
            set_semantic_scope_active_metadata(metadata);
        } else {
            // Preserve semantic attribution under unexpectedly deep compiler-
            // inserted nesting without allocating from inside the allocator ABI.
            // The fixed overflow stack covers another bounded set of scopes;
            // pushes beyond that bound become explicit unknown/fallback scopes
            // until their matching pops, avoiding false attribution to the last
            // representable scope while keeping all outer scopes restoreable.
            push_overflow_semantic_scope_metadata(metadata);
        }
    }
}

/// Push a thread-local semantic metadata scope for compiler-inserted MIR.
///
/// This is the fast ABI used by the rustc_driver MIR pass.  It avoids moving a
/// full `AllocationMetadata` value through the C ABI on every allocation-site
/// scope.  The previous scope is stored in a bounded thread-local stack and is
/// restored by `__unialloc_semantic_scope_pop`.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_push(
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) {
    push_semantic_scope_metadata(ffi_metadata(type_id, module_id, flags, callsite));
}

/// Push a compiler-proven local semantic metadata scope.
///
/// This variant is selected only when the MIR pass also pairs the corresponding
/// allocation, drop, and local realloc scopes.  It preserves active metadata for
/// ordinary `GlobalAlloc` calls but marks the scope so the allocator can skip
/// recovery-record bookkeeping on the hot path.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_push_local(
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
) {
    push_semantic_scope_metadata(local_scope_metadata(ffi_metadata(
        type_id, module_id, flags, callsite,
    )));
}

/// Push a thread-local semantic metadata scope with full metadata hints.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_push_hints(
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) {
    push_semantic_scope_metadata(ffi_metadata_with_hints(
        type_id,
        module_id,
        flags,
        lifetime_hint,
        placement_hint,
        callsite,
    ));
}

/// Push a compiler-proven local semantic metadata scope with full hints.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_push_hints_local(
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    callsite: u64,
) {
    push_semantic_scope_metadata(local_scope_metadata(ffi_metadata_with_hints(
        type_id,
        module_id,
        flags,
        lifetime_hint,
        placement_hint,
        callsite,
    )));
}

/// Pop the previous thread-local semantic metadata scope.
#[no_mangle]
pub extern "C" fn __unialloc_semantic_scope_pop() {
    unsafe {
        if pop_overflow_semantic_scope_metadata() {
            return;
        }
        let depth = SEMANTIC_SCOPE_STACK_DEPTH;
        if depth == 0 {
            set_semantic_scope_active_metadata(AllocationMetadata::unknown());
            SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS = false;
            return;
        }
        let next_depth = depth - 1;
        if next_depth == 0 {
            if SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS {
                set_semantic_scope_active_metadata(SEMANTIC_SCOPE_STACK[0]);
                SEMANTIC_SCOPE_STACK[0] = AllocationMetadata::unknown();
                SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS = false;
            } else {
                set_semantic_scope_active_metadata(AllocationMetadata::unknown());
            }
        } else {
            set_semantic_scope_active_metadata(SEMANTIC_SCOPE_STACK[next_depth]);
            SEMANTIC_SCOPE_STACK[next_depth] = AllocationMetadata::unknown();
        }
        SEMANTIC_SCOPE_STACK_DEPTH = next_depth;
    }
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;
    use core::alloc::GlobalAlloc;
    use core::mem::{align_of, size_of, size_of_val, MaybeUninit};
    use std::boxed::Box;
    use std::thread;
    use std::vec::Vec;

    fn test_guard() -> SemanticTestGuard {
        semantic_test_guard()
    }

    fn assert_delayed_free_snapshot_accounting(snapshot: DelayedFreeSnapshot) {
        assert_eq!(
            snapshot.tracked_retained_bytes, snapshot.retained_bytes,
            "delayed-free hot-path retained-byte counter must match slot scan"
        );
        assert!(
            snapshot.retained_accounting_matches,
            "delayed-free snapshot should flag retained-byte accounting drift"
        );
    }

    fn first_unrounded_type_cache_layout_for_test() -> Layout {
        let mut size = MIN_TYPE_CACHE_OBJECT_SIZE;
        while size <= MIN_TYPE_CACHE_OBJECT_SIZE + 256 {
            let layout = Layout::from_size_align(size, align_of::<usize>()).unwrap();
            if type_cache_retained_bytes_for_layout(layout) > size {
                return layout;
            }
            size += 1;
        }
        panic!("test size classes must include an internally rounded semantic-cache object");
    }

    struct SemanticStateCleanup;

    impl Drop for SemanticStateCleanup {
        fn drop(&mut self) {
            unsafe {
                clear_type_cache_for_test();
                clear_delayed_free_for_test();
            }
            semantic_stats_recording_disable();
        }
    }

    unsafe fn reset_semantic_scope_stack_for_test() {
        restore_active_metadata(AllocationMetadata::unknown());
        SEMANTIC_SCOPE_STACK = [AllocationMetadata::unknown(); SEMANTIC_SCOPE_STACK_CAPACITY];
        SEMANTIC_SCOPE_STACK_DEPTH = 0;
        SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH = 0;
        SEMANTIC_SCOPE_OVERFLOW_STACK =
            [AllocationMetadata::unknown(); SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY];
        SEMANTIC_SCOPE_OVERFLOW_BOUNDARY_ACTIVE = AllocationMetadata::unknown();
        SEMANTIC_SCOPE_STACK_BASE_HAS_PREVIOUS = false;
    }

    unsafe fn clear_scoped_metadata_gate_for_test() {
        restore_active_metadata(AllocationMetadata::unknown());
        semantic_slow_path_clear(SLOW_PATH_SCOPED_METADATA_MASK);
    }

    unsafe fn clear_type_cache_for_test() {
        TYPE_CACHE = [TypeCacheSlot::empty(); TYPE_CACHE_SLOTS];
        TYPE_CACHE_HOT_SLOT = TypeCacheHotSlot::empty();
        clear_type_cache_identity_hot_slot_for_test();
        INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        SEGREGATED_TYPE_CACHE = [SegregatedTypeCacheBucket::empty(); TYPE_CACHE_SLOTS];
        SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
        SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
        SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
        INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry::empty();
        INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND = SegregatedTypeCacheEntry::empty();
        #[cfg(not(feature = "fixed_heap"))]
        {
            HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry::empty();
            HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND = SegregatedTypeCacheEntry::empty();
        }
        SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket::empty();
        SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.store(0, Ordering::Relaxed);
        SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.store(0, Ordering::Relaxed);
        clear_auto_layout_metadata_hot_slot();
        clear_hugepage_segregated_type_cache_for_test();
        clear_memory_tags_for_test();
        clear_auto_allocation_records();
    }

    unsafe fn clear_type_cache_identity_hot_slot_for_test() {
        TYPE_CACHE_IDENTITY_HOT_SLOT = TypeCacheIdentityHotSlot::empty();
        TYPE_CACHE_IDENTITY_HOT_HITS.store(0, Ordering::Relaxed);
    }

    /// Copy test-observable `static mut` state without creating a Rust
    /// reference to the static.
    ///
    /// The production allocator owns this state as thread-local mutable
    /// storage.  These tests run under `semantic_test_guard()`, so taking a
    /// by-value raw-pointer snapshot is enough for assertions and keeps the
    /// tests compatible with Rust 2024's `static_mut_refs` lint.  The helper is
    /// deliberately test-local: allocator hot paths should keep using their
    /// existing narrow raw-pointer or local-borrow helpers.
    #[inline]
    unsafe fn snapshot_static_copy<T: Copy>(ptr: *const T) -> T {
        ptr.read()
    }

    #[inline]
    unsafe fn type_cache_snapshot_for_test() -> [TypeCacheSlot; TYPE_CACHE_SLOTS] {
        snapshot_static_copy(core::ptr::addr_of!(TYPE_CACHE))
    }

    #[inline]
    unsafe fn type_cache_slot_snapshot_for_test(idx: usize) -> TypeCacheSlot {
        debug_assert!(idx < TYPE_CACHE_SLOTS);
        snapshot_static_copy((core::ptr::addr_of!(TYPE_CACHE) as *const TypeCacheSlot).add(idx))
    }

    #[inline]
    unsafe fn type_cache_hot_slot_snapshot_for_test() -> TypeCacheHotSlot {
        snapshot_static_copy(core::ptr::addr_of!(TYPE_CACHE_HOT_SLOT))
    }

    #[inline]
    unsafe fn inline_type_cache_entry_snapshot_for_test() -> InlineTypeCacheEntry {
        snapshot_static_copy(core::ptr::addr_of!(INLINE_TYPE_CACHE_ENTRY))
    }

    #[inline]
    unsafe fn inline_segregated_type_cache_entry_snapshot_for_test(
        cache_domain: u8,
    ) -> SegregatedTypeCacheEntry {
        snapshot_static_copy(inline_segregated_type_cache_entry(cache_domain) as *const _)
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[inline]
    unsafe fn hugepage_inline_segregated_type_cache_entry_snapshot_for_test(
    ) -> SegregatedTypeCacheEntry {
        inline_segregated_type_cache_entry_snapshot_for_test(SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE)
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[inline]
    unsafe fn hugepage_second_inline_segregated_type_cache_entry_snapshot_for_test(
    ) -> SegregatedTypeCacheEntry {
        snapshot_static_copy(core::ptr::addr_of!(
            HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_ENTRY_SECOND
        ))
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[inline]
    unsafe fn hugepage_inline_segregated_type_cache_entries_snapshot_for_test(
    ) -> [SegregatedTypeCacheEntry; HUGEPAGE_INLINE_SEGREGATED_TYPE_CACHE_DEPTH] {
        [
            hugepage_inline_segregated_type_cache_entry_snapshot_for_test(),
            hugepage_second_inline_segregated_type_cache_entry_snapshot_for_test(),
        ]
    }

    #[inline]
    unsafe fn ordinary_inline_segregated_type_cache_entry_snapshot_for_test(
    ) -> SegregatedTypeCacheEntry {
        inline_segregated_type_cache_entry_snapshot_for_test(SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY)
    }

    #[inline]
    unsafe fn segregated_type_cache_bucket_snapshot_for_test(
        idx: usize,
    ) -> SegregatedTypeCacheBucket {
        debug_assert!(idx < TYPE_CACHE_SLOTS);
        snapshot_static_copy(
            (core::ptr::addr_of!(SEGREGATED_TYPE_CACHE) as *const SegregatedTypeCacheBucket)
                .add(idx),
        )
    }

    #[inline]
    unsafe fn segregated_type_cache_hot_bucket_snapshot_for_test() -> SegregatedTypeCacheHotBucket {
        snapshot_static_copy(core::ptr::addr_of!(SEGREGATED_TYPE_CACHE_HOT_BUCKET))
    }

    #[inline]
    unsafe fn fast_auto_allocation_record_inline_snapshot_for_test() -> AutoAllocationRecord {
        snapshot_static_copy(core::ptr::addr_of!(FAST_AUTO_ALLOCATION_RECORD_INLINE))
    }

    #[inline]
    unsafe fn fast_auto_allocation_record_hot_slot_snapshot_for_test(
    ) -> FastAutoAllocationRecordHotSlot {
        snapshot_static_copy(core::ptr::addr_of!(FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT))
    }

    #[inline]
    unsafe fn memory_tag_hot_slot_snapshot_for_test() -> MemoryTagHotSlot {
        snapshot_static_copy(core::ptr::addr_of!(MEMORY_TAG_HOT_SLOT))
    }

    #[inline]
    unsafe fn delayed_free_slots_snapshot_for_test() -> [DelayedFreeSlot; DELAYED_FREE_SLOTS] {
        snapshot_static_copy(core::ptr::addr_of!(DELAYED_FREE))
    }

    #[inline]
    unsafe fn semantic_scope_stack_depth_for_test() -> usize {
        snapshot_static_copy(core::ptr::addr_of!(SEMANTIC_SCOPE_STACK_DEPTH))
    }

    #[inline]
    unsafe fn semantic_scope_stack_overflow_depth_for_test() -> usize {
        snapshot_static_copy(core::ptr::addr_of!(SEMANTIC_SCOPE_STACK_OVERFLOW_DEPTH))
    }

    fn reset_metadata_auth_cookie_for_test() {
        #[cfg(not(unialloc_target_arm64e))]
        unsafe {
            METADATA_RECORD_AUTH_HOT_SLOT = MetadataRecordAuthHotSlot::empty();
        }
        METADATA_RECORD_AUTH_SEED.store(0, Ordering::Relaxed);
        METADATA_AUTH_COOKIE.store(0, Ordering::Relaxed);
        METADATA_RECORD_AUTH_SEED_DERIVATIONS.store(0, Ordering::Relaxed);
        METADATA_RECORD_AUTH_COMPUTATIONS.store(0, Ordering::Relaxed);
    }

    unsafe fn cached_segregated_type_cache_entry_for_test(
        layout: Layout,
        metadata: AllocationMetadata,
        ptr: *mut u8,
    ) -> Option<*mut SegregatedTypeCacheEntry> {
        let cache_key = type_cache_identity_key(metadata);
        let policy_key = segregated_type_cache_policy_key(metadata);
        let inline_cache_domain = segregated_type_cache_inline_domain(metadata);
        let mut inline_index = 0usize;
        while inline_index < inline_segregated_type_cache_capacity(inline_cache_domain) {
            let inline_entry =
                inline_segregated_type_cache_entry_at(inline_cache_domain, inline_index);
            if (*inline_entry).ptr == ptr
                && (*inline_entry).matches_cached_key(layout, cache_key, policy_key)
            {
                return Some(inline_entry);
            }
            inline_index += 1;
        }

        let bucket = &mut SEGREGATED_TYPE_CACHE[segregated_type_cache_slot(metadata, layout)];
        let mut idx = 0usize;
        while idx < bucket.count {
            if bucket.entries[idx].ptr == ptr
                && bucket.entries[idx].matches_cached_key(layout, cache_key, policy_key)
            {
                return Some(&mut bucket.entries[idx] as *mut SegregatedTypeCacheEntry);
            }
            idx += 1;
        }
        None
    }

    #[test]
    fn fast_auto_allocation_record_builder_keeps_unprotected_hot_path_unsigned() {
        let _guard = test_guard();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let ptr = 0x4000_1000usize as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC003_0001)
            .with_module(0xC0DE)
            .with_callsite(0xA110_0001)
            .with_flags(FLAG_TYPE_ISOLATED);

        let record = fast_auto_allocation_record_for(ptr, layout, metadata);

        assert_eq!(record.ptr, ptr as usize);
        assert_eq!(record.size, layout.size());
        assert_eq!(record.align, layout.align());
        assert_eq!(record.metadata, metadata);
        assert_eq!(
            record.auth, 0,
            "same-thread compiler recovery records should not pay integrity hashing unless a protected metadata policy requests it"
        );
        assert!(fast_auto_allocation_record_auth_matches(
            ptr, layout, record
        ));
    }

    #[test]
    fn fast_auto_allocation_record_builder_preserves_protected_auth() {
        let _guard = test_guard();
        reset_metadata_auth_cookie_for_test();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let ptr = 0x4000_2000usize as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC003_0002)
            .with_module(0xC0DE)
            .with_callsite(0xA110_0002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION);

        let record = fast_auto_allocation_record_for(ptr, layout, metadata);

        assert_ne!(
            record.auth, 0,
            "protected metadata must keep an authenticator even on the TLS fast record path"
        );
        assert_eq!(
            record.auth,
            derive_auto_allocation_record_auth(ptr, layout, metadata)
        );
        assert!(fast_auto_allocation_record_auth_matches(
            ptr, layout, record
        ));

        let tampered = AutoAllocationRecord {
            metadata: metadata.with_callsite(0xD0D0_0002),
            ..record
        };
        assert!(
            !fast_auto_allocation_record_auth_matches(ptr, layout, tampered),
            "metadata-protected recovery records must reject stale/tampered metadata"
        );
    }

    #[test]
    fn fast_auto_allocation_record_tls_cache_has_bounded_footprint() {
        let _guard = test_guard();
        let bytes = size_of::<[AutoAllocationRecord; FAST_AUTO_ALLOCATION_RECORD_SLOTS]>();
        assert!(
            FAST_AUTO_ALLOCATION_RECORD_SLOTS.is_power_of_two(),
            "open-addressed TLS recovery cache requires a power-of-two slot count"
        );
        assert!(
            bytes <= 16 * 1024,
            "same-thread recovery cache should stay a small TLS hot cache, not a large per-thread side table"
        );
        assert!(
            FAST_AUTO_ALLOCATION_RECORD_SLOTS < AUTO_ALLOCATION_RECORD_SLOTS,
            "global sharded recovery table remains the spill path for many live records"
        );
    }

    #[test]
    fn fast_auto_allocation_record_tls_cache_spills_to_global_when_full() {
        let _guard = test_guard();
        clear_auto_allocation_records();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5A11)
            .with_module(0xC0DE_5A11)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            // The inline slot plus the bounded TLS array should handle the
            // first small same-thread set without taking the global shard
            // locks.  Extra live records must still be accepted by spilling to
            // the existing process-visible sharded table rather than silently
            // dropping compiler metadata.
            let total = FAST_AUTO_ALLOCATION_RECORD_SLOTS + 17;
            let mut idx = 0usize;
            while idx < total {
                let ptr = (0x7200_0000usize + (idx + 1) * 0x1000) as *mut u8;
                assert!(record_recovery_auto_allocation_metadata_eligible(
                    ptr,
                    layout,
                    metadata.with_callsite(idx as u64)
                ));
                idx += 1;
            }
            assert!(
                FAST_AUTO_ALLOCATION_RECORD_COUNT <= FAST_AUTO_ALLOCATION_RECORD_SLOTS,
                "TLS recovery cache must remain bounded even when more records are accepted"
            );
            assert!(
                AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed) > 0,
                "records beyond the bounded TLS cache should spill to the global recovery table"
            );
            clear_auto_allocation_records();
        }
    }

    #[test]
    fn recovery_identity_matches_allocator_identity_but_not_callsite() {
        let recorded = AllocationMetadata::for_type(0xC003_1D01)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED)
            .with_lifetime_hint(7)
            .with_placement_hint(11)
            .with_callsite(0xA110_C001);
        let drop_site = recorded.with_callsite(0xD00D_C001);
        let wrong_module = drop_site.with_module(0xBAD0);
        let layout_derived = AllocationMetadata {
            placement_hint: LAYOUT_DERIVED_PLACEMENT_HINT,
            ..drop_site
        };

        assert!(allocation_metadata_recovery_identity_matches(
            drop_site, recorded
        ));
        assert!(
            !allocation_metadata_recovery_identity_matches(wrong_module, recorded),
            "module/context is an allocation identity boundary"
        );
        assert!(
            !allocation_metadata_recovery_identity_matches(layout_derived, recorded),
            "layout-derived metadata must not be promoted to exact compiler recovery identity"
        );
    }

    #[test]
    fn recovery_identity_mismatch_is_observable_and_uses_recorded_identity() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(
            4 * core::mem::size_of::<usize>(),
            core::mem::align_of::<usize>(),
        )
        .unwrap();
        let recorded = AllocationMetadata::for_type(0xC003_7101)
            .with_module(0xC003_7102)
            .with_callsite(0xC003_7103)
            .with_flags(FLAG_TYPE_ISOLATED);
        let wrong_drop_site = AllocationMetadata::for_type(0xC003_7104)
            .with_module(recorded.module_id)
            .with_callsite(0xC003_7105)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, recorded) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, wrong_drop_site);
        }

        let validation = semantic_metadata_validation_snapshot();
        assert_eq!(validation.recovery_identity_mismatches, 1);
        assert_eq!(
            validation.last_mismatch_requested_type_id,
            wrong_drop_site.type_id
        );
        assert_eq!(validation.last_mismatch_recorded_type_id, recorded.type_id);
        assert_eq!(
            validation.last_mismatch_requested_callsite,
            wrong_drop_site.callsite
        );
        assert_eq!(
            validation.last_mismatch_recorded_callsite,
            recorded.callsite
        );

        let reused = unsafe { alloc.alloc_with_metadata(layout, recorded) };
        assert_eq!(
            reused, ptr,
            "mismatched deallocation metadata must not poison the wrong type cache"
        );
        unsafe {
            alloc.dealloc_with_metadata(reused, layout, recorded);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn type_cache_identity_ignores_callsite_but_keeps_allocator_boundaries() {
        let base = AllocationMetadata::for_type(0xC003_1D02)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_lifetime_hint(7)
            .with_placement_hint(11)
            .with_callsite(0xA110_C001);
        let drop_site = base.with_callsite(0xD00D_C001);
        let different_type = AllocationMetadata {
            type_id: 0xC003_1D03,
            ..base
        };

        assert_eq!(
            type_cache_identity_key(base),
            type_cache_identity_key(drop_site),
            "allocation and Drop sites for the same compiler object class must share reuse state"
        );
        assert_ne!(
            type_cache_identity_key(base),
            type_cache_identity_key(different_type),
            "type id remains a cache isolation boundary"
        );
        assert_ne!(
            type_cache_identity_key(base),
            type_cache_identity_key(base.with_module(0xBAD0)),
            "module/context remains a cache isolation boundary"
        );
        assert_ne!(
            type_cache_identity_key(base),
            type_cache_identity_key(base.with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED)),
            "allocator policy flags remain cache isolation boundaries"
        );
        assert_ne!(
            type_cache_identity_key(base),
            type_cache_identity_key(base.with_lifetime_hint(8)),
            "lifetime hint remains a cache isolation boundary"
        );
        assert_ne!(
            type_cache_identity_key(base),
            type_cache_identity_key(base.with_placement_hint(12)),
            "placement hint remains a cache isolation boundary"
        );
    }

    #[test]
    fn type_cache_identity_hot_slot_reuses_callsite_agnostic_key() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_identity_hot_slot_for_test();
        }

        let base = AllocationMetadata::for_type(0xC003_1D04)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_lifetime_hint(7)
            .with_placement_hint(11)
            .with_callsite(0xA110_C002);
        let drop_site = base.with_callsite(0xD00D_C002);

        let allocation_key = type_cache_identity_key(base);
        assert_eq!(TYPE_CACHE_IDENTITY_HOT_HITS.load(Ordering::Relaxed), 0);

        let drop_key = type_cache_identity_key(drop_site);
        assert_eq!(
            allocation_key, drop_key,
            "callsite-only metadata changes must keep the same cache identity"
        );
        assert_eq!(
            TYPE_CACHE_IDENTITY_HOT_HITS.load(Ordering::Relaxed),
            1,
            "callsite-only metadata changes should hit the identity hot slot"
        );

        let different_module = base.with_module(0xBAD0);
        assert_ne!(
            allocation_key,
            type_cache_identity_key(different_module),
            "module/context changes must still miss and compute a distinct identity"
        );
        assert_eq!(
            TYPE_CACHE_IDENTITY_HOT_HITS.load(Ordering::Relaxed),
            1,
            "metadata-boundary changes must not be counted as hot-slot hits"
        );

        let same_module_drop_site = different_module.with_callsite(0xD00D_C003);
        assert_eq!(
            type_cache_identity_key(different_module),
            type_cache_identity_key(same_module_drop_site),
            "the refilled hot slot should also be callsite agnostic"
        );
        assert!(
            TYPE_CACHE_IDENTITY_HOT_HITS.load(Ordering::Relaxed) >= 2,
            "refilled identity hot slot should serve later callsite-only variants"
        );
    }

    unsafe fn clear_hugepage_segregated_type_cache_for_test() {
        #[cfg(not(feature = "fixed_heap"))]
        if !HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
            (*HUGEPAGE_SEGREGATED_TYPE_CACHE) =
                [SegregatedTypeCacheBucket::empty(); TYPE_CACHE_SLOTS];
            HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    unsafe fn drop_hugepage_segregated_type_cache_for_test() {
        if !HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
            let mapping_size = if HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE == 0 {
                size_of::<[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]>()
            } else {
                HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE
            };
            system_alloc::munmap(HUGEPAGE_SEGREGATED_TYPE_CACHE as *mut u8, mapping_size);
            HUGEPAGE_SEGREGATED_TYPE_CACHE = core::ptr::null_mut();
            HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE = 0;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES = 0;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_RETAINED_BYTES_TRUSTED = false;
            HUGEPAGE_SEGREGATED_TYPE_CACHE_BACKING = system_alloc::HugePageMmapBacking::Unmapped;
        }
        SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket::empty();
    }

    #[cfg(not(feature = "fixed_heap"))]
    unsafe fn hugepage_segregated_bucket_count_for_test(
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> Option<usize> {
        if HUGEPAGE_SEGREGATED_TYPE_CACHE.is_null() {
            return None;
        }
        Some((*HUGEPAGE_SEGREGATED_TYPE_CACHE)[segregated_type_cache_slot(metadata, layout)].count)
    }

    #[cfg(not(feature = "fixed_heap"))]
    unsafe fn hugepage_segregated_occupied_bucket_count_for_test() -> usize {
        HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn assert_hugepage_metadata_mapping_size_matches_backing(
        snapshot: HugepageMetadataSideCacheSnapshot,
    ) {
        let table_size = size_of::<[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]>();
        assert!(
            snapshot.mapping_size >= table_size,
            "side-cache mapping must fit the bucket table: mapping={} table={}",
            snapshot.mapping_size,
            table_size
        );

        if snapshot.backing.is_hugepage() {
            assert!(
                snapshot.mapping_size >= HUGEPAGE_METADATA_MAPPING_GRANULARITY,
                "real hugepage backing should keep hugepage granularity"
            );
        } else if snapshot.backing.is_fallback() {
            let compact_size =
                compact_metadata_mapping_size(table_size).expect("table size should round");
            assert_eq!(
                snapshot.mapping_size, compact_size,
                "ordinary fallback backing should retain only the compact page-rounded table"
            );
            assert!(
                snapshot.mapping_size < HUGEPAGE_METADATA_MAPPING_GRANULARITY,
                "ordinary fallback must not pin a hugepage-sized side-cache mapping"
            );
        } else {
            panic!(
                "live side-cache must report hugepage or fallback backing, got {:?}",
                snapshot.backing
            );
        }
    }

    fn assert_same_pointer_set<const N: usize>(expected: [*mut u8; N], observed: [*mut u8; N]) {
        let mut expected = expected.map(|ptr| ptr as usize);
        let mut observed = observed.map(|ptr| ptr as usize);
        expected.sort_unstable();
        observed.sort_unstable();
        assert_eq!(observed, expected, "cached object pointer set changed");
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_compact_fallback_size_is_page_rounded() {
        let table_size = size_of::<[SegregatedTypeCacheBucket; TYPE_CACHE_SLOTS]>();
        let compact_size =
            compact_metadata_mapping_size(table_size).expect("table size should round");
        let hugepage_size =
            hugepage_metadata_mapping_size(table_size).expect("table size should round");

        assert!(compact_size >= table_size);
        assert_eq!(compact_size & (crate::PAGE_SIZE - 1), 0);
        assert_eq!(hugepage_size, HUGEPAGE_METADATA_MAPPING_GRANULARITY);
        assert!(
            compact_size < hugepage_size,
            "ordinary fallback side-cache mapping should not retain a whole hugepage"
        );
    }

    unsafe fn clear_memory_tags_for_test() {
        MEMORY_TAGS = [TaggedAllocation::empty(); MEMORY_TAG_FAST_SLOTS];
        MEMORY_TAG_HOT_SLOT = MemoryTagHotSlot::empty();
        MEMORY_TAG_RECORD_COUNT = 0;
        for shard in GLOBAL_MEMORY_TAGS.iter() {
            let mut global = shard.lock();
            #[cfg(not(feature = "fixed_heap"))]
            {
                let mut page = global.overflow;
                while !page.is_null() {
                    let next = (*page).next;
                    system_alloc::munmap(page as *mut u8, size_of::<MemoryTagPage>());
                    page = next;
                }
            }
            *global = GlobalMemoryTagTable::empty();
        }
        GLOBAL_MEMORY_TAG_RECORD_COUNT.store(0, Ordering::Relaxed);
        #[cfg(not(feature = "fixed_heap"))]
        {
            let mut page = MEMORY_TAG_OVERFLOW;
            while !page.is_null() {
                let next = (*page).next;
                system_alloc::munmap(page as *mut u8, size_of::<MemoryTagPage>());
                page = next;
            }
            MEMORY_TAG_OVERFLOW = core::ptr::null_mut();
        }
    }

    unsafe fn clear_delayed_free_for_test() {
        DELAYED_FREE = [DelayedFreeSlot::empty(); DELAYED_FREE_SLOTS];
        DELAYED_FREE_CURSOR = 0;
        DELAYED_FREE_RETAINED_BYTES = 0;
        DELAYED_FREE_OCCUPIED_MASK = 0;
    }

    unsafe fn release_delayed_free_for_test(alloc: &RustAllocator) {
        let mut idx = 0usize;
        while idx < DELAYED_FREE_SLOTS {
            let slot = DELAYED_FREE[idx];
            DELAYED_FREE[idx] = DelayedFreeSlot::empty();
            if !slot.is_empty() {
                release_delayed_slot(alloc, slot);
            }
            idx += 1;
        }
        DELAYED_FREE_CURSOR = 0;
        DELAYED_FREE_RETAINED_BYTES = 0;
        DELAYED_FREE_OCCUPIED_MASK = 0;
    }

    #[cfg(feature = "fixed_heap")]
    unsafe fn fill_auto_allocation_record_tables_for_test(
        layout: Layout,
        metadata: AllocationMetadata,
    ) {
        let fast_inline_ptr = 0x4000_0000usize as *mut u8;
        FAST_AUTO_ALLOCATION_RECORD_INLINE =
            fast_auto_allocation_record_for(fast_inline_ptr, layout, metadata);
        let mut idx = 0;
        while idx < FAST_AUTO_ALLOCATION_RECORD_SLOTS {
            let ptr = (0x4100_0000usize + (idx + 1) * align_of::<usize>()) as *mut u8;
            FAST_AUTO_ALLOCATION_RECORDS[idx] =
                fast_auto_allocation_record_for(ptr, layout, metadata);
            idx += 1;
        }
        FAST_AUTO_ALLOCATION_RECORD_COUNT = FAST_AUTO_ALLOCATION_RECORD_SLOTS;
        FAST_AUTO_ALLOCATION_RECORD_HOT_SLOT = FastAutoAllocationRecordHotSlot::empty();

        let mut global_idx = 0;
        for table_lock in AUTO_ALLOCATION_RECORDS.iter() {
            let mut table = table_lock.lock();
            let mut shard_idx = 0;
            while shard_idx < AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
                let ptr = (0x5000_0000usize + (global_idx + 1) * align_of::<usize>()) as *mut u8;
                table.inline[shard_idx] = AutoAllocationRecord {
                    ptr: ptr as usize,
                    size: layout.size(),
                    align: layout.align(),
                    auth: derive_auto_allocation_record_auth(ptr, layout, metadata),
                    metadata,
                };
                shard_idx += 1;
                global_idx += 1;
            }
        }
        AUTO_ALLOCATION_RECORD_COUNT.store(AUTO_ALLOCATION_RECORD_SLOTS, Ordering::Relaxed);
        semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);
    }

    unsafe fn drain_semantic_cache_for_test(
        alloc: &RustAllocator,
        layout: Layout,
        metadata: AllocationMetadata,
    ) {
        while let Some(ptr) = pop_semantic_type_cache(layout, metadata) {
            alloc.dealloc_raw(ptr, layout);
        }
    }

    unsafe fn metadata_segregated_growth_bucket_available_for_test(
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> bool {
        let start = segregated_type_cache_slot(metadata, layout);
        let mut offset = 0usize;
        while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
            let bucket_idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
            let bucket = &SEGREGATED_TYPE_CACHE[bucket_idx];
            if !bucket.is_corrupt()
                && bucket.count < SEGREGATED_TYPE_CACHE_DEPTH
                && bucket.can_accept_object_size(layout.size())
            {
                return true;
            }
            offset += 1;
        }
        false
    }

    unsafe fn drain_segregated_probe_window_for_test(alloc: &RustAllocator, start: usize) {
        let mut offset = 0;
        while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
            let bucket_idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
            let bucket = &mut SEGREGATED_TYPE_CACHE[bucket_idx];
            let mut entry_idx = 0;
            while entry_idx < bucket.count {
                let entry = bucket.entries[entry_idx];
                if !entry.ptr.is_null() {
                    let layout = checked_side_table_layout(
                        entry.size,
                        entry.align,
                        "test segregated metadata",
                    );
                    alloc.dealloc_raw(entry.ptr, layout);
                }
                bucket.entries[entry_idx] = SegregatedTypeCacheEntry::empty();
                entry_idx += 1;
            }
            bucket.count = 0;
            bucket.evict_cursor = 0;
            bucket.retained_bytes = 0;
            offset += 1;
        }
    }

    fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
        SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }
    }

    fn global_auto_allocation_record_location_for_test(ptr: *mut u8) -> Option<(usize, usize)> {
        let ptr_key = ptr as usize;
        let (home_shard_idx, home_start) = auto_allocation_record_shard_and_slot(ptr);
        let mut shard_offset = 0;
        while shard_offset < AUTO_ALLOCATION_RECORD_SHARD_COUNT {
            let shard_idx = next_auto_allocation_record_shard(home_shard_idx, shard_offset);
            let start = if shard_offset == 0 { home_start } else { 0 };
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            let mut offset = 0;
            while offset < AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
                let idx = (start + offset) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1);
                let record = table.inline[idx];
                if record.ptr == ptr_key {
                    return Some((shard_idx, idx));
                }
                if record.is_empty() {
                    break;
                }
                offset += 1;
            }
            shard_offset += 1;
        }
        None
    }

    #[test]
    fn metadata_builders_keep_compatibility_defaults() {
        let _guard = test_guard();
        let metadata = AllocationMetadata::for_type(7)
            .with_module(11)
            .with_callsite(13)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        assert!(metadata.has_type());
        assert!(metadata.requests(FLAG_TYPE_ISOLATED));
        assert!(metadata.requests(FLAG_METADATA_SEGREGATED));
        assert_eq!(metadata.module_id, 11);
        assert_eq!(metadata.callsite, 13);
    }

    #[test]
    fn rust_type_ids_are_non_zero_and_type_specific() {
        let _guard = test_guard();
        let u64_id = semantic_type_id::<u64>();
        let pair_id = semantic_type_id::<(u64, u64)>();
        assert_ne!(u64_id, UNKNOWN_SEMANTIC_ID);
        assert_ne!(u64_id, pair_id);
        assert_eq!(AllocationMetadata::for_rust_type::<u64>().type_id, u64_id);
    }

    #[test]
    fn layout_ids_are_stable_and_layout_specific() {
        let _guard = test_guard();
        let a = semantic_layout_id(64, align_of::<usize>());
        let b = semantic_layout_id(64, align_of::<usize>());
        let c = semantic_layout_id(128, align_of::<usize>());
        assert_ne!(a, UNKNOWN_SEMANTIC_ID);
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn auto_metadata_synthesizes_layout_semantics_when_enabled() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        assert_eq!(auto_allocation_metadata(layout), None);

        semantic_auto_metadata_enable(0x11, FLAG_DELAYED_FREE, 0x22);
        let metadata = auto_allocation_metadata(layout).expect("auto metadata enabled");
        assert_eq!(
            metadata.type_id,
            semantic_layout_id(96, align_of::<usize>())
        );
        assert_eq!(metadata.module_id, 0x11);
        assert_eq!(metadata.callsite, 0x22);
        assert!(metadata.is_layout_derived());
        assert!(metadata.requests(FLAG_TYPE_ISOLATED));
        assert!(metadata.requests(FLAG_DELAYED_FREE));

        let dealloc_metadata =
            auto_deallocation_metadata(layout).expect("auto deallocation metadata enabled");
        assert_eq!(dealloc_metadata.type_id, metadata.type_id);
        assert!(dealloc_metadata.is_layout_derived());
        assert!(dealloc_metadata.requests(FLAG_DELAYED_FREE));

        semantic_auto_metadata_disable();
        assert_eq!(auto_allocation_metadata(layout), None);
        assert_eq!(auto_deallocation_metadata(layout), None);
    }

    #[test]
    fn layout_auto_metadata_hot_slot_preserves_compiler_stream_semantics() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        semantic_auto_metadata_enable(0x11, FLAG_DELAYED_FREE, 0x22);
        let metadata = auto_allocation_metadata(layout).expect("layout metadata");
        assert!(metadata.is_layout_derived());
        unsafe {
            let cached = AUTO_LAYOUT_METADATA_HOT_SLOT;
            assert!(cached.matches(
                layout,
                metadata.flags,
                metadata.module_id,
                metadata.callsite
            ));
            assert_eq!(cached.metadata, metadata);
        }
        assert_eq!(
            auto_deallocation_metadata(layout).expect("cached deallocation metadata"),
            metadata,
            "layout alloc/dealloc should share the same hot metadata key"
        );

        let ids = [0xC002_00A1_u64, 0xC002_00A2_u64];
        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0x11,
                FLAG_DELAYED_FREE,
                0x22,
                ids.as_ptr(),
                ids.len(),
            )
        });
        unsafe {
            AUTO_LAYOUT_METADATA_HOT_SLOT = AutoLayoutMetadataHotSlot {
                size: layout.size(),
                align: layout.align(),
                flags: metadata.flags,
                module_id: metadata.module_id,
                callsite: metadata.callsite,
                metadata,
            };
        }

        let first = auto_allocation_metadata(layout).expect("first compiler stream id");
        let dealloc_before_exhaustion =
            auto_deallocation_metadata(layout).expect("compiler-mode deallocation metadata");
        let second = auto_allocation_metadata(layout).expect("second compiler stream id");
        let exhausted = auto_allocation_metadata(layout);
        let dealloc_after_exhaustion = auto_deallocation_metadata(layout);

        assert_eq!(first.type_id, ids[0]);
        assert_eq!(second.type_id, ids[1]);
        assert!(!first.is_layout_derived());
        assert!(!second.is_layout_derived());
        assert_eq!(first.callsite, 0x22);
        assert_eq!(second.callsite, 0x23);
        assert_eq!(exhausted, None);
        assert_eq!(dealloc_before_exhaustion.type_id, UNKNOWN_SEMANTIC_ID);
        assert!(!dealloc_before_exhaustion.is_layout_derived());
        assert_eq!(
            dealloc_after_exhaustion, None,
            "after finite compiler stream exhaustion, unrecorded raw fallback allocations must not inherit synthetic compiler-mode deallocation metadata"
        );

        semantic_auto_metadata_disable();
    }

    #[test]
    fn compiler_auto_metadata_replays_allocation_site_ids() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        let ids = [0xC002_0001_u64, 0xC002_0002_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_enable(
                0xC0DE,
                FLAG_DELAYED_FREE,
                0xA110_C000,
                ids.as_ptr(),
                ids.len(),
            )
        });
        assert!(semantic_auto_metadata_enabled());
        assert!(semantic_auto_compiler_metadata_enabled());
        assert_eq!(
            semantic_auto_metadata_type_id_basis(),
            "compiler-assigned-allocation-site-object-type-id-cyclic-replay"
        );

        let first = auto_allocation_metadata(layout).expect("first replay id");
        let second = auto_allocation_metadata(layout).expect("second replay id");
        let third = auto_allocation_metadata(layout).expect("cyclic replay id");

        assert_eq!(first.type_id, ids[0]);
        assert_eq!(second.type_id, ids[1]);
        assert_eq!(third.type_id, ids[0]);
        assert_eq!(first.module_id, 0xC0DE);
        assert_eq!(first.callsite, 0xA110_C000);
        assert_eq!(second.callsite, 0xA110_C001);
        assert!(!first.is_layout_derived());
        assert!(first.requests(FLAG_TYPE_ISOLATED));
        assert!(first.requests(FLAG_DELAYED_FREE));

        semantic_auto_metadata_disable();
        assert!(!semantic_auto_metadata_enabled());
        assert!(!semantic_auto_compiler_metadata_enabled());
        assert_eq!(semantic_auto_metadata_type_id_basis(), "none");
    }

    #[test]
    fn compiler_auto_metadata_thread_local_replay_uses_tls_cursor() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        let ids = [0xC003_7A10u64, 0xC003_7A11u64];
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_thread_local_recovery_enable(
                0xC0DE_7A10,
                FLAG_TYPE_ISOLATED,
                0xA110_7A10,
                ids.as_ptr(),
                ids.len(),
            )
        });
        assert_eq!(AUTO_COMPILER_TYPE_IDS_CURSOR.load(Ordering::Relaxed), 0);
        assert_eq!(
            auto_allocation_metadata(layout).map(|metadata| metadata.type_id),
            Some(ids[0])
        );
        assert_eq!(
            auto_allocation_metadata(layout).map(|metadata| metadata.type_id),
            Some(ids[1])
        );
        assert_eq!(
            auto_allocation_metadata(layout).map(|metadata| metadata.type_id),
            Some(ids[0])
        );
        assert_eq!(
            AUTO_COMPILER_TYPE_IDS_CURSOR.load(Ordering::Relaxed),
            0,
            "thread-local replay should not pay or mutate the global atomic cursor"
        );

        semantic_auto_metadata_disable();
        assert!(unsafe {
            semantic_auto_compiler_metadata_enable(
                0xC0DE_7A10,
                FLAG_TYPE_ISOLATED,
                0xA110_7A10,
                ids.as_ptr(),
                ids.len(),
            )
        });
        assert_eq!(
            auto_allocation_metadata(layout).map(|metadata| metadata.type_id),
            Some(ids[0])
        );
        assert_eq!(
            AUTO_COMPILER_TYPE_IDS_CURSOR.load(Ordering::Relaxed),
            1,
            "conservative/global replay still uses the process-visible atomic cursor"
        );
        semantic_auto_metadata_disable();
    }

    #[test]
    fn compiler_auto_metadata_stream_consumes_without_wrapping() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();
        let ids = [0xC002_1001_u64, 0xC002_1002_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_DELAYED_FREE,
                0xA110_C100,
                ids.as_ptr(),
                ids.len(),
            )
        });
        assert!(semantic_auto_metadata_enabled());
        assert!(semantic_auto_compiler_metadata_enabled());
        assert!(semantic_auto_compiler_metadata_stream_enabled());
        assert_eq!(
            semantic_auto_metadata_type_id_basis(),
            "compiler-assigned-allocation-site-object-type-id-consuming-stream"
        );

        let first = auto_allocation_metadata(layout).expect("first stream id");
        let second = auto_allocation_metadata(layout).expect("second stream id");
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_AUTO_METADATA,
            0,
            "consuming the final compiler id should clear the auto-metadata slow-path flag"
        );
        assert!(
            !semantic_allocation_slow_path_enabled(),
            "ordinary allocations should stop entering the auto-metadata slow path immediately after the final finite compiler id is consumed"
        );
        let exhausted = auto_allocation_metadata(layout);
        let exhausted_again = auto_allocation_metadata(layout);

        assert_eq!(first.type_id, ids[0]);
        assert_eq!(second.type_id, ids[1]);
        assert_eq!(first.callsite, 0xA110_C100);
        assert_eq!(second.callsite, 0xA110_C101);
        assert_eq!(exhausted, None);
        assert_eq!(exhausted_again, None);
        assert_eq!(
            AUTO_COMPILER_TYPE_IDS_CURSOR.load(Ordering::Relaxed),
            ids.len(),
            "consuming-stream exhaustion should not keep advancing the cursor on later ordinary allocations"
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "without outstanding recovery records, exhausted compiler streams should not keep ordinary runtime dealloc/realloc on the semantic slow path"
        );
        assert!(
            !semantic_allocation_slow_path_enabled(),
            "ordinary allocations should not keep paying the auto-metadata slow path once a finite compiler stream is exhausted"
        );
        assert_eq!(
            auto_deallocation_metadata(layout),
            None,
            "a raw allocation made after stream exhaustion must not be deallocated with synthetic fallback semantic policy"
        );

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_DELAYED_FREE,
                0xA110_C120,
                ids.as_ptr(),
                ids.len(),
            )
        });
        let reenrolled = auto_allocation_metadata(layout).expect("stream id after re-enable");
        assert_eq!(reenrolled.type_id, ids[0]);
        assert_eq!(reenrolled.callsite, 0xA110_C120);
        assert_ne!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_AUTO_METADATA,
            0,
            "re-enabling a finite compiler stream should restore the auto-metadata slow-path flag until its final id is consumed"
        );
        assert!(semantic_allocation_slow_path_enabled());
        assert!(semantic_runtime_slow_path_enabled());

        semantic_auto_metadata_disable();
        assert!(!semantic_auto_metadata_enabled());
        assert!(!semantic_auto_compiler_metadata_enabled());
        assert!(!semantic_auto_compiler_metadata_stream_enabled());
    }

    #[test]
    fn compiler_auto_deallocation_metadata_does_not_consume_stream_ids() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2001_u64, 0xC002_2002_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_DELAYED_FREE,
                0xA110_C200,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let first_alloc = auto_allocation_metadata(layout).expect("first stream id");
        let first_dealloc =
            auto_deallocation_metadata(layout).expect("deallocation policy metadata");
        let second_alloc = auto_allocation_metadata(layout).expect("second stream id");
        let exhausted = auto_allocation_metadata(layout);

        assert_eq!(first_alloc.type_id, ids[0]);
        assert_eq!(second_alloc.type_id, ids[1]);
        assert_eq!(first_dealloc.type_id, UNKNOWN_SEMANTIC_ID);
        assert!(first_dealloc.requests(FLAG_TYPE_ISOLATED));
        assert!(first_dealloc.requests(FLAG_DELAYED_FREE));
        assert_eq!(exhausted, None);

        semantic_auto_metadata_disable();
    }

    #[test]
    fn exhausted_compiler_stream_does_not_semantically_dealloc_unrecorded_raw_pointer() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2051_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE,
                0xA110_C250,
                ids.as_ptr(),
                ids.len(),
            )
        });
        let metadata = auto_allocation_metadata(layout).expect("single stream id");
        assert_eq!(metadata.type_id, ids[0]);
        assert_eq!(auto_allocation_metadata(layout), None);
        assert!(!semantic_allocation_slow_path_enabled());
        assert!(!semantic_runtime_slow_path_enabled());
        assert_eq!(auto_deallocation_metadata(layout), None);

        let alloc = RustAllocator::new();
        let before = semantic_stats_snapshot();
        let ptr = unsafe { alloc.alloc_raw(layout) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc(ptr, layout);
        }
        let after = semantic_stats_snapshot();
        assert_eq!(
            after.typed_deallocations + after.fallback_deallocations,
            before.typed_deallocations + before.fallback_deallocations,
            "unrecorded raw dealloc after stream exhaustion should bypass semantic accounting and policy"
        );
        assert_eq!(
            after.total_deallocations, before.total_deallocations,
            "independent total dealloc counter must stay unchanged for unrecorded raw pointers"
        );

        semantic_auto_metadata_disable();
    }

    #[test]
    fn compiler_auto_deallocation_metadata_recovers_recorded_stream_id() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2101_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C210,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        let recovered =
            take_auto_deallocation_metadata(ptr, layout).expect("recorded auto metadata");
        assert_eq!(recovered.type_id, ids[0]);
        assert_eq!(recovered.module_id, 0xC0DE);
        assert_eq!(recovered.callsite, 0xA110_C210);
        assert_eq!(auto_allocation_metadata(layout), None);

        semantic_auto_metadata_disable();
        unsafe {
            alloc.dealloc_raw(ptr, layout);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn compiler_auto_metadata_default_recovery_remains_global() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2121_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C212,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            1,
            "the stable public compiler-replay API stays conservative: records are globally visible unless a caller explicitly asks for thread-local replay"
        );
        assert_eq!(
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed),
            0,
            "default global replay should not use the TLS fast-record counter"
        );
        let recovered =
            take_auto_deallocation_metadata(ptr, layout).expect("recorded auto metadata");
        assert_eq!(recovered.type_id, ids[0]);
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        semantic_auto_metadata_disable();
        unsafe {
            alloc.dealloc_raw(ptr, layout);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn compiler_auto_metadata_thread_local_recovery_uses_fast_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2122_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_thread_local_recovery_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C213,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "thread-local replay should avoid the global recovery table on the single-thread benchmark hot path"
        );
        assert_eq!(
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed),
            1,
            "thread-local replay still records exact allocation metadata for same-thread deallocation"
        );
        let recovered =
            take_auto_deallocation_metadata(ptr, layout).expect("fast recorded auto metadata");
        assert_eq!(recovered.type_id, ids[0]);
        assert_eq!(
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed),
            0
        );

        semantic_auto_metadata_disable();
        unsafe {
            alloc.dealloc_raw(ptr, layout);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn compiler_stream_realloc_to_untyped_fallback_removes_old_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2151_u64];
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let new_size = 4096;
        let new_layout = Layout::from_size_align(new_size, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata {
            type_id: ids[0],
            module_id: 0xC0DE,
            flags: FLAG_TYPE_ISOLATED,
            lifetime_hint: 0,
            placement_hint: 0,
            callsite: 0xA110_C215,
        };

        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C215,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout).map(|metadata| metadata.type_id),
            Some(ids[0])
        );
        assert!(
            semantic_runtime_slow_path_enabled(),
            "the recorded old allocation must keep realloc/dealloc recovery active after stream exhaustion"
        );
        assert_eq!(auto_allocation_metadata(new_layout), None);

        let new_ptr = unsafe { alloc.realloc(ptr, layout, new_size) };
        assert!(!new_ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "the old typed recovery record must be consumed by fallback realloc"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(new_ptr, new_layout),
            None,
            "the new allocation has no compiler stream id and must remain raw/untyped rather than inheriting stale metadata"
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "after fallback realloc consumes the final old record, exhausted compiler-stream mode should not slow later raw deallocations"
        );

        unsafe {
            alloc.dealloc(new_ptr, new_layout);
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
    }

    #[test]
    fn explicit_semantic_alloc_does_not_create_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_22F1)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C2F1)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "explicit SemanticAlloc callers pass metadata on dealloc and should not populate the recovery side table"
        );
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);

        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "explicit SemanticAlloc should not leave record-only recovery state active"
        );
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn compiler_abi_alloc_records_metadata_for_ordinary_global_dealloc() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_22F2)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C2F2)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(metadata),
            "compiler ABI allocations must leave a recovery record for ordinary Box/drop deallocation"
        );
        assert!(
            semantic_runtime_slow_path_enabled(),
            "outstanding compiler ABI recovery records must keep ordinary GlobalAlloc dealloc on the semantic slow path"
        );

        unsafe {
            alloc.dealloc(ptr, layout);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn compiler_abi_alloc_hints_preserve_full_recovery_metadata() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_22F3)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C2F3)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x31)
            .with_placement_hint(0x42);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata_hints(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(metadata),
            "metadata-complete compiler ABI must retain lifetime/placement hints in recovery records"
        );
        assert!(unsafe {
            __unialloc_dealloc_with_metadata_hints(
                ptr,
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        });
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.fallback_deallocations, 0);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
    }

    #[test]
    fn scoped_metadata_allocation_recovers_dealloc_after_scope_exit() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_2301)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C230)
            .with_flags(FLAG_TYPE_ISOLATED);

        let previous = unsafe { set_active_metadata(metadata) };
        let ptr = unsafe { alloc.alloc(layout) };
        unsafe {
            restore_active_metadata(previous);
        }
        assert!(!ptr.is_null());
        assert!(
            semantic_runtime_slow_path_enabled(),
            "an outstanding compiler-scoped allocation record must keep dealloc recovery active"
        );

        semantic_stats_recording_disable();
        assert!(
            semantic_runtime_slow_path_enabled(),
            "recorded metadata, not stats accounting, should keep dealloc/realloc recovery active"
        );
        assert!(
            !semantic_allocation_slow_path_enabled(),
            "record-only recovery state should not slow unrelated future allocations"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout).map(|metadata| metadata.type_id),
            Some(metadata.type_id),
            "the scoped allocation should be recoverable by pointer after the scope exits"
        );
        let before_raw_alloc = semantic_stats_snapshot();
        let unrelated = unsafe { alloc.alloc(layout) };
        assert!(!unrelated.is_null());
        assert_eq!(
            semantic_stats_snapshot().total_allocations,
            before_raw_alloc.total_allocations,
            "record-only state should bypass semantic allocation accounting"
        );
        unsafe {
            alloc.dealloc(unrelated, layout);
            alloc.dealloc(ptr, layout);
        }
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "the recovered deallocation should remove the final metadata record; flags={} records={}",
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed),
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed)
        );
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
        semantic_stats_recording_disable();
    }
    #[cfg(feature = "stats")]
    #[test]
    fn scoped_metadata_dealloc_prefers_recorded_allocation_metadata_over_current_scope() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let allocated_as = AllocationMetadata::for_type(0xC002_2401)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C240)
            .with_flags(FLAG_TYPE_ISOLATED);
        let unrelated_scope = AllocationMetadata::for_type(0xC002_2402)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C241)
            .with_flags(FLAG_TYPE_ISOLATED);

        let previous = unsafe { set_active_metadata(allocated_as) };
        let ptr = unsafe { alloc.alloc(layout) };
        unsafe {
            restore_active_metadata(previous);
        }
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout).map(|metadata| metadata.type_id),
            Some(allocated_as.type_id)
        );

        let previous = unsafe { set_active_metadata(unrelated_scope) };
        unsafe {
            alloc.dealloc(ptr, layout);
            restore_active_metadata(previous);
        }

        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "dealloc should clear the recorded allocation metadata instead of leaking it behind the current scope"
        );

        let mut rows = [empty_type_stats_row(); 4];
        let row_count = semantic_type_stats_snapshot(&mut rows);
        assert!(row_count >= 1);
        let allocated_row = rows
            .iter()
            .find(|row| row.type_id == allocated_as.type_id)
            .expect("allocated type row");
        assert_eq!(allocated_row.allocations, 1);
        assert_eq!(allocated_row.deallocations, 1);
        assert!(
            rows.iter()
                .all(|row| row.type_id != unrelated_scope.type_id),
            "unrelated active scope must not receive the deallocation"
        );
        semantic_stats_recording_disable();
    }
    #[cfg(feature = "stats")]
    #[test]
    fn scoped_metadata_realloc_splits_recorded_old_type_from_current_new_scope() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC002_2501)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C250)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC002_2502)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C251)
            .with_flags(FLAG_TYPE_ISOLATED);

        let previous = unsafe { set_active_metadata(old_metadata) };
        let ptr = unsafe { alloc.alloc(old_layout) };
        unsafe {
            restore_active_metadata(previous);
        }
        assert!(!ptr.is_null());
        unsafe {
            ptr.write(0xA5);
            ptr.add(old_layout.size() - 1).write(0x5A);
        }

        let new_size = old_layout.size() * 2;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let previous = unsafe { set_active_metadata(new_metadata) };
        let new_ptr = unsafe { alloc.realloc(ptr, old_layout, new_size) };
        unsafe {
            restore_active_metadata(previous);
        }
        assert!(!new_ptr.is_null());
        unsafe {
            assert_eq!(new_ptr.read(), 0xA5);
            assert_eq!(new_ptr.add(old_layout.size() - 1).read(), 0x5A);
        }
        assert_eq!(lookup_auto_allocation_metadata(ptr, old_layout), None);
        assert_eq!(
            lookup_auto_allocation_metadata(new_ptr, new_layout).map(|metadata| metadata.type_id),
            Some(new_metadata.type_id),
            "realloc should record the current scope as the new object's metadata"
        );

        unsafe {
            alloc.dealloc(new_ptr, new_layout);
        }
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "the new object's recovered deallocation should clear the final metadata record"
        );

        let mut rows = [empty_type_stats_row(); 8];
        let _ = semantic_type_stats_snapshot(&mut rows);
        let old_row = rows
            .iter()
            .find(|row| row.type_id == old_metadata.type_id)
            .expect("old type row");
        let new_row = rows
            .iter()
            .find(|row| row.type_id == new_metadata.type_id)
            .expect("new type row");
        assert_eq!(old_row.allocations, 1);
        assert_eq!(old_row.deallocations, 1);
        assert_eq!(new_row.allocations, 1);
        assert_eq!(new_row.deallocations, 1);
        semantic_stats_recording_disable();
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "after stats are disabled, no leaked metadata record should keep the runtime slow path active"
        );
    }

    #[test]
    fn compiler_auto_allocation_records_preserve_hash_collision_overflow() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let ids = [0xC002_2201_u64];
        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C220,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let first_ptr = (0x1000usize << 4) as *mut u8;
        let target_slot = auto_allocation_record_slot(first_ptr);
        let mut ptrs = [core::ptr::null_mut(); AUTO_ALLOCATION_RECORD_PROBE_LIMIT + 1];
        let mut found = 0;
        let mut candidate = 0x1000usize;
        while found < ptrs.len() {
            let ptr = (candidate << 4) as *mut u8;
            if auto_allocation_record_slot(ptr) == target_slot {
                ptrs[found] = ptr;
                found += 1;
            }
            candidate += 1;
        }

        for (idx, ptr) in ptrs.iter().copied().enumerate() {
            let metadata = AllocationMetadata::for_type(0xC002_2201 + idx as u64)
                .with_module(0xC0DE)
                .with_callsite(0xA110_C220 + idx as u64)
                .with_flags(FLAG_TYPE_ISOLATED);
            unsafe {
                record_auto_allocation_metadata(ptr, layout, metadata);
            }
        }

        #[cfg(not(feature = "fixed_heap"))]
        {
            assert!(
                global_auto_allocation_record_location_for_test(
                    ptrs[AUTO_ALLOCATION_RECORD_PROBE_LIMIT]
                )
                .is_some(),
                "the first record beyond the home-shard bounded probe window should spill into a later shard before mmap overflow"
            );
        }
        #[cfg(feature = "fixed_heap")]
        {
            assert!(
                global_auto_allocation_record_location_for_test(
                    ptrs[AUTO_ALLOCATION_RECORD_PROBE_LIMIT]
                )
                .is_some(),
                "fixed_heap has no overflow page, so it must retain colliding records inside the sharded inline budget"
            );
        }

        for (idx, ptr) in ptrs.iter().copied().enumerate() {
            let metadata =
                take_auto_deallocation_metadata(ptr, layout).expect("recorded collision metadata");
            assert_eq!(metadata.type_id, 0xC002_2201 + idx as u64);
            assert_eq!(metadata.callsite, 0xA110_C220 + idx as u64);
        }

        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
    }

    #[test]
    fn auto_allocation_records_are_sharded_without_reducing_capacity() {
        let _guard = test_guard();
        clear_auto_allocation_records();

        assert_eq!(
            AUTO_ALLOCATION_RECORD_SHARD_COUNT * AUTO_ALLOCATION_RECORD_SHARD_SLOTS,
            AUTO_ALLOCATION_RECORD_SLOTS,
            "global recovery sharding must preserve the old total inline record budget"
        );
        assert_eq!(
            AUTO_ALLOCATION_RECORDS.len(),
            AUTO_ALLOCATION_RECORD_SHARD_COUNT
        );
        for shard in AUTO_ALLOCATION_RECORDS.iter() {
            assert_eq!(
                shard.lock().inline.len(),
                AUTO_ALLOCATION_RECORD_SHARD_SLOTS
            );
        }
    }

    #[test]
    fn auto_allocation_record_active_shard_mask_tracks_insert_and_final_remove() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x3910usize << 4) as *mut u8;
        let shard_idx = auto_allocation_record_shard(ptr);
        let metadata = AllocationMetadata::for_type(0xC002_3910)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C391)
            .with_flags(FLAG_TYPE_ISOLATED);

        assert_eq!(
            auto_allocation_record_active_shards(),
            0,
            "a cleared recovery table should not advertise active shards"
        );
        unsafe {
            assert!(record_global_auto_allocation_metadata(
                ptr, layout, metadata
            ));
        }
        assert!(
            auto_allocation_record_shard_is_active(
                auto_allocation_record_active_shards(),
                shard_idx
            ),
            "inserting the first record should publish only its shard bit"
        );
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), Some(metadata));
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "active-shard skipping must still recover and consume the record"
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert_eq!(
            auto_allocation_record_active_shards(),
            0,
            "removing the final live record should clear the sparse-shard bitset"
        );
    }

    #[test]
    fn auto_allocation_record_shard_live_count_keeps_active_until_last_remove() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let first_ptr = (0x3B10usize << 4) as *mut u8;
        let shard_idx = auto_allocation_record_shard(first_ptr);
        let second_ptr = (0x3B11usize..0x3C80usize)
            .map(|candidate| (candidate << 4) as *mut u8)
            .find(|candidate| {
                auto_allocation_record_shard(*candidate) == shard_idx
                    && auto_allocation_record_shard_and_slot(*candidate).1
                        != auto_allocation_record_shard_and_slot(first_ptr).1
            })
            .expect("test pointer from same recovery shard but another slot");
        let first_metadata = AllocationMetadata::for_type(0xC002_3B10)
            .with_module(0xC0DE)
            .with_callsite(0xA110_3B10)
            .with_flags(FLAG_TYPE_ISOLATED);
        let second_metadata = AllocationMetadata::for_type(0xC002_3B11)
            .with_module(0xC0DE)
            .with_callsite(0xA110_3B11)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            assert!(record_global_auto_allocation_metadata(
                first_ptr,
                layout,
                first_metadata
            ));
            assert!(record_global_auto_allocation_metadata(
                second_ptr,
                layout,
                second_metadata
            ));
        }
        {
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_eq!(
                table.live_count, 2,
                "normal inserts should maintain O(1) shard live-count metadata"
            );
        }
        assert_eq!(
            take_auto_deallocation_metadata(first_ptr, layout),
            Some(first_metadata)
        );
        {
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_eq!(
                table.live_count, 1,
                "non-final removal should update live_count without scanning the whole shard"
            );
            assert!(
                auto_allocation_record_shard_is_active(
                    auto_allocation_record_active_shards(),
                    shard_idx
                ),
                "active bit must remain set while one live record remains in the shard"
            );
        }
        assert_eq!(
            take_auto_deallocation_metadata(second_ptr, layout),
            Some(second_metadata)
        );
        {
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_eq!(table.live_count, 0);
        }
        assert!(
            !auto_allocation_record_shard_is_active(
                auto_allocation_record_active_shards(),
                shard_idx
            ),
            "removing the last shard record should clear the active bit"
        );
    }

    #[test]
    fn auto_allocation_record_active_offsets_iterate_only_set_shards_from_home() {
        let active_mask = auto_allocation_record_shard_bit(6)
            | auto_allocation_record_shard_bit(0)
            | auto_allocation_record_shard_bit(3)
            | (1usize << (AUTO_ALLOCATION_RECORD_SHARD_COUNT + 3));
        let mut active_offsets = auto_allocation_record_active_offsets_from_home(active_mask, 6);

        assert_eq!(
            pop_next_auto_allocation_record_active_shard(6, &mut active_offsets),
            Some((6, 0)),
            "the home shard should be probed first when it is active"
        );
        assert_eq!(
            pop_next_auto_allocation_record_active_shard(6, &mut active_offsets),
            Some((0, 2)),
            "iteration should wrap through active shards without visiting inactive ones"
        );
        assert_eq!(
            pop_next_auto_allocation_record_active_shard(6, &mut active_offsets),
            Some((3, 5))
        );
        assert_eq!(
            pop_next_auto_allocation_record_active_shard(6, &mut active_offsets),
            None,
            "bits outside the shard mask must not create bogus shard visits"
        );
    }

    #[test]
    fn auto_allocation_record_recover_includes_home_when_active_mask_stale() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x3C10usize << 4) as *mut u8;
        let home_shard_idx = auto_allocation_record_shard(ptr);
        let stale_active_shard_idx = next_auto_allocation_record_shard(home_shard_idx, 1);
        let metadata = AllocationMetadata::for_type(0xC002_3C10)
            .with_module(0xC0DE)
            .with_callsite(0xA110_3C10)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            assert!(record_global_auto_allocation_metadata(
                ptr, layout, metadata
            ));
        }
        assert!(
            auto_allocation_record_shard_is_active(
                auto_allocation_record_active_shards(),
                home_shard_idx
            ),
            "normal insert should publish the home shard bit"
        );

        let stale_mask = auto_allocation_record_shard_bit(stale_active_shard_idx);
        AUTO_ALLOCATION_RECORD_ACTIVE_SHARDS.store(stale_mask, Ordering::Release);
        assert!(
            !auto_allocation_record_shard_is_active(
                auto_allocation_record_active_shards(),
                home_shard_idx
            ),
            "test deliberately simulates a stale nonzero active mask that omits the home shard"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(metadata),
            "lookup must include the pointer's home shard even when the active-shard hint is stale"
        );
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "removal must use the same home-including probe policy as lookup"
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert_eq!(
            auto_allocation_record_active_shards(),
            0,
            "final removal should restore a clean active-shard mask"
        );
    }

    #[test]
    fn auto_allocation_record_inactive_home_shard_is_reused_before_other_active_shards() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let target_ptr = (0x3A10usize << 4) as *mut u8;
        let (home_shard_idx, _) = auto_allocation_record_shard_and_slot(target_ptr);
        let active_ptr = (0x3A10usize..0x3B10usize)
            .map(|candidate| (candidate << 4) as *mut u8)
            .find(|candidate| auto_allocation_record_shard(*candidate) != home_shard_idx)
            .expect("test pointer from a different recovery shard");
        let active_shard_idx = auto_allocation_record_shard(active_ptr);
        let active_metadata = AllocationMetadata::for_type(0xC002_3A01)
            .with_module(0xC0DE)
            .with_callsite(0xA110_3A01)
            .with_flags(FLAG_TYPE_ISOLATED);
        let target_metadata = AllocationMetadata::for_type(0xC002_3A10)
            .with_module(0xC0DE)
            .with_callsite(0xA110_3A10)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            assert!(record_global_auto_allocation_metadata(
                active_ptr,
                layout,
                active_metadata
            ));
        }
        let active_mask = auto_allocation_record_active_shards();
        assert!(
            auto_allocation_record_shard_is_active(active_mask, active_shard_idx),
            "the seed record should make an unrelated shard active"
        );
        assert!(
            !auto_allocation_record_shard_is_active(active_mask, home_shard_idx),
            "the target home shard should still be inactive before insertion"
        );

        unsafe {
            assert!(
                record_global_auto_allocation_metadata(target_ptr, layout, target_metadata),
                "active-mask insertion should still probe an inactive home shard first"
            );
        }
        let (record_shard_idx, _record_idx) =
            global_auto_allocation_record_location_for_test(target_ptr)
                .expect("target recovery record");
        assert_eq!(
            record_shard_idx, home_shard_idx,
            "an empty/inactive home shard is the lowest-contention reusable target even when another shard is active"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(target_ptr, layout),
            Some(target_metadata)
        );
        assert_eq!(
            take_auto_deallocation_metadata(active_ptr, layout),
            Some(active_metadata),
            "the unrelated active-shard record must remain recoverable"
        );
        assert_eq!(
            take_auto_deallocation_metadata(target_ptr, layout),
            Some(target_metadata),
            "the newly installed inactive-home record must be recoverable"
        );

        clear_auto_allocation_records();
    }

    #[test]
    fn auto_allocation_record_shard_overflow_preserves_inline_capacity() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x3900usize << 4) as *mut u8;
        let (home_shard_idx, _) = auto_allocation_record_shard_and_slot(ptr);
        let next_shard_idx = next_auto_allocation_record_shard(home_shard_idx, 1);
        let metadata = AllocationMetadata::for_type(0xC002_3901)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C390)
            .with_flags(FLAG_TYPE_ISOLATED);

        {
            let mut home = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
            for idx in 0..AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
                let dummy_ptr = ((0x3900_0000usize + idx) << 4) as *mut u8;
                let dummy_metadata = AllocationMetadata::for_type(0xC002_3900 + idx as u64)
                    .with_module(0xC0DE)
                    .with_callsite(0xA110_3900 + idx as u64)
                    .with_flags(FLAG_TYPE_ISOLATED);
                home.inline[idx] = AutoAllocationRecord {
                    ptr: dummy_ptr as usize,
                    size: layout.size(),
                    align: layout.align(),
                    auth: derive_auto_allocation_record_auth(dummy_ptr, layout, dummy_metadata),
                    metadata: dummy_metadata,
                };
            }
        }
        AUTO_ALLOCATION_RECORD_COUNT.store(AUTO_ALLOCATION_RECORD_SHARD_SLOTS, Ordering::Relaxed);
        semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);

        unsafe {
            assert!(
                record_global_auto_allocation_metadata(ptr, layout, metadata),
                "a full home shard should spill into another shard before dropping recovery metadata"
            );
        }
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            AUTO_ALLOCATION_RECORD_SHARD_SLOTS + 1
        );
        let (record_shard_idx, _record_idx) =
            global_auto_allocation_record_location_for_test(ptr).expect("inserted shard record");
        assert_ne!(
            record_shard_idx, home_shard_idx,
            "the overflow record should move out of the contended home shard"
        );
        assert_eq!(
            record_shard_idx, next_shard_idx,
            "the first overflow shard should be deterministic"
        );
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), Some(metadata));
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "recovery lookup must scan shard-overflow records, not only the home shard"
        );
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            AUTO_ALLOCATION_RECORD_SHARD_SLOTS
        );

        clear_auto_allocation_records();
    }

    #[test]
    fn compiler_auto_allocation_records_preserve_probe_chain_after_delete() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let first_ptr = (0x3000usize << 4) as *mut u8;
        let target_slot = auto_allocation_record_slot(first_ptr);
        let mut ptrs = [core::ptr::null_mut(); 2];
        let mut found = 0;
        let mut candidate = 0x3000usize;
        while found < ptrs.len() {
            let ptr = (candidate << 4) as *mut u8;
            if auto_allocation_record_slot(ptr) == target_slot {
                ptrs[found] = ptr;
                found += 1;
            }
            candidate += 1;
        }

        let first_metadata = AllocationMetadata::for_type(0xC002_2301)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C230)
            .with_flags(FLAG_TYPE_ISOLATED);
        let second_metadata = AllocationMetadata::for_type(0xC002_2302)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C231)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            record_auto_allocation_metadata(ptrs[0], layout, first_metadata);
            record_auto_allocation_metadata(ptrs[1], layout, second_metadata);
        }
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 2);

        assert_eq!(
            take_auto_deallocation_metadata(ptrs[0], layout),
            Some(first_metadata)
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 1);
        assert_eq!(
            lookup_auto_allocation_metadata(ptrs[1], layout),
            Some(second_metadata),
            "deleting an earlier colliding record must leave a tombstone so later records remain reachable"
        );
        assert_eq!(
            take_auto_deallocation_metadata(ptrs[1], layout),
            Some(second_metadata)
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        {
            let (shard_idx, slot_idx) = auto_allocation_record_shard_and_slot(ptrs[0]);
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert!(
                table.inline[slot_idx].is_tombstone(),
                "the earlier deletion should keep its tombstone until the slot is reused"
            );
            assert!(
                table.inline[(slot_idx + 1) & (AUTO_ALLOCATION_RECORD_SHARD_SLOTS - 1)].is_empty(),
                "deleting the final live record should clear only the removed slot in O(1)"
            );
        }

        let replacement_metadata = AllocationMetadata::for_type(0xC002_2303)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C232)
            .with_flags(FLAG_TYPE_ISOLATED);
        unsafe {
            record_auto_allocation_metadata(ptrs[0], layout, replacement_metadata);
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptrs[0], layout),
            Some(replacement_metadata),
            "a later insert should reuse the tombstone left by a prior colliding delete"
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        semantic_auto_metadata_disable();
    }

    #[test]
    fn compiler_auto_allocation_global_hot_slot_tracks_recent_inline_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x3500usize << 4) as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC002_2401)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C240)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            assert!(
                record_auto_allocation_metadata(ptr, layout, metadata),
                "global recovery record insertion should succeed"
            );
        }
        {
            let shard_idx = auto_allocation_record_shard(ptr);
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_eq!(table.hot.ptr, ptr as usize);
            assert!(table.hot.slot_idx < AUTO_ALLOCATION_RECORD_SHARD_SLOTS);
            assert_eq!(table.inline[table.hot.slot_idx].ptr, ptr as usize);
        }

        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), Some(metadata));
        {
            let shard_idx = auto_allocation_record_shard(ptr);
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_eq!(
                table.hot.ptr, ptr as usize,
                "lookup should preserve the inline hot slot for repeated recovery probes"
            );
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "hot-slot recovery must still consume the record correctly"
        );
        {
            let shard_idx = auto_allocation_record_shard(ptr);
            let table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_ne!(
                table.hot.ptr, ptr as usize,
                "removing the record should invalidate its hot-slot cache entry"
            );
        }
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        clear_auto_allocation_records();
    }

    #[test]
    fn compiler_auto_allocation_global_remove_saturates_stale_zero_count() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x3510usize << 4) as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC002_24FF)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C24F)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            assert!(record_global_auto_allocation_metadata(
                ptr, layout, metadata
            ));
        }
        AUTO_ALLOCATION_RECORD_COUNT.store(0, Ordering::Relaxed);
        {
            let shard_idx = auto_allocation_record_shard(ptr);
            let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            let idx = global_auto_allocation_record_hot_slot(&*table, ptr as usize)
                .expect("recorded inline hot slot");
            remove_global_auto_allocation_inline_record(shard_idx, &mut *table, idx, ptr as usize);
            assert!(
                table.inline[idx].is_empty(),
                "stale zero count should make the removed slot empty, not leave a tombstone"
            );
        }

        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "stale zero count must not underflow to usize::MAX"
        );
        clear_auto_allocation_records();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn compiler_auto_allocation_records_spill_to_overflow_when_global_table_is_full() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        {
            let mut global_idx = 0;
            for table_lock in AUTO_ALLOCATION_RECORDS.iter() {
                let mut table = table_lock.lock();
                for shard_idx in 0..AUTO_ALLOCATION_RECORD_SHARD_SLOTS {
                    let dummy_ptr = ((0x1000_0000usize + global_idx) << 4) as *mut u8;
                    let dummy_metadata =
                        AllocationMetadata::for_type(0xC002_F000 + global_idx as u64)
                            .with_module(0xC0DE)
                            .with_callsite(0xA110_F000 + global_idx as u64)
                            .with_flags(FLAG_TYPE_ISOLATED);
                    table.inline[shard_idx] = AutoAllocationRecord {
                        ptr: dummy_ptr as usize,
                        size: layout.size(),
                        align: layout.align(),
                        auth: derive_auto_allocation_record_auth(dummy_ptr, layout, dummy_metadata),
                        metadata: dummy_metadata,
                    };
                    global_idx += 1;
                }
            }
        }
        AUTO_ALLOCATION_RECORD_COUNT.store(AUTO_ALLOCATION_RECORD_SLOTS, Ordering::Relaxed);

        let overflow_ptr = (0x2000_0000usize << 4) as *mut u8;
        let overflow_metadata = AllocationMetadata::for_type(0xC002_FFFF)
            .with_module(0xC0DE)
            .with_callsite(0xA110_FFFF)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x77)
            .with_placement_hint(0x88);

        unsafe {
            record_auto_allocation_metadata(overflow_ptr, layout, overflow_metadata);
        }
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            AUTO_ALLOCATION_RECORD_SLOTS + 1,
            "a full global recovery table must not silently drop compiler allocation metadata"
        );
        {
            let home_shard_idx = auto_allocation_record_shard(overflow_ptr);
            let table = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
            assert!(
                !table.overflow.is_null(),
                "overflow page should be allocated in the home shard for exact recovery records beyond the sharded inline table"
            );
            assert!(
                find_auto_allocation_record_in_overflow(table.overflow, overflow_ptr as usize)
                    .is_some(),
                "the spill record should be discoverable in overflow"
            );
        }
        assert_eq!(
            lookup_auto_allocation_metadata(overflow_ptr, layout),
            Some(overflow_metadata)
        );
        assert_eq!(
            take_auto_deallocation_metadata(overflow_ptr, layout),
            Some(overflow_metadata)
        );
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            AUTO_ALLOCATION_RECORD_SLOTS
        );
        {
            let home_shard_idx = auto_allocation_record_shard(overflow_ptr);
            let table = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
            assert!(
                table.overflow.is_null(),
                "the last consumed overflow recovery record should release its cold mmap page"
            );
        }

        clear_auto_allocation_records();
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn compiler_auto_allocation_overflow_page_survives_until_last_live_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let first_ptr = (0x2100_0000usize << 4) as *mut u8;
        let home_shard_idx = auto_allocation_record_shard(first_ptr);
        let mut second_key = 0x2100_0001usize;
        while auto_allocation_record_shard((second_key << 4) as *mut u8) != home_shard_idx {
            second_key += 1;
            assert!(
                second_key < 0x2100_1000,
                "test fixture should find two synthetic pointers for the same recovery shard"
            );
        }
        let second_ptr = (second_key << 4) as *mut u8;
        let first_metadata = AllocationMetadata::for_type(0xC002_E001)
            .with_module(0xC0DE)
            .with_callsite(0xA110_E001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION);
        let second_metadata = AllocationMetadata::for_type(0xC002_E002)
            .with_module(0xC0DE)
            .with_callsite(0xA110_E002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x22);

        unsafe {
            let page = allocate_auto_allocation_record_page();
            assert!(
                !page.is_null(),
                "hosted test needs one recovery overflow page"
            );
            (*page).entries[0] = auto_allocation_record_for(first_ptr, layout, first_metadata);
            (*page).entries[1] = auto_allocation_record_for(second_ptr, layout, second_metadata);
            let mut table = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
            (*page).next = table.overflow;
            table.overflow = page;
            table.live_count = 2;
        }
        AUTO_ALLOCATION_RECORD_COUNT.store(2, Ordering::Relaxed);
        auto_allocation_record_mark_shard_active(home_shard_idx);
        semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);

        assert_eq!(
            lookup_auto_allocation_metadata(first_ptr, layout),
            Some(first_metadata)
        );
        assert_eq!(
            lookup_auto_allocation_metadata(second_ptr, layout),
            Some(second_metadata)
        );
        assert_eq!(
            take_auto_deallocation_metadata(first_ptr, layout),
            Some(first_metadata)
        );
        {
            let table = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
            assert!(
                !table.overflow.is_null(),
                "an overflow page with another live recovery record must not be unmapped"
            );
            assert!(
                find_auto_allocation_record_in_overflow(table.overflow, second_ptr as usize)
                    .is_some(),
                "the remaining live overflow record must stay recoverable"
            );
        }
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 1);

        assert_eq!(
            take_auto_deallocation_metadata(second_ptr, layout),
            Some(second_metadata)
        );
        {
            let table = AUTO_ALLOCATION_RECORDS[home_shard_idx].lock();
            assert!(
                table.overflow.is_null(),
                "the overflow page should be unmapped once its last live record is consumed"
            );
            assert_eq!(
                table.live_count, 0,
                "release should not leave the recovery shard marked live"
            );
        }
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        clear_auto_allocation_records();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn global_recovery_ignores_impossible_non_home_overflow_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x2200_0000usize << 4) as *mut u8;
        let home_shard_idx = auto_allocation_record_shard(ptr);
        let non_home_shard_idx = next_auto_allocation_record_shard(home_shard_idx, 1);
        let metadata = AllocationMetadata::for_type(0xC002_F1A7)
            .with_module(0xC0DE)
            .with_callsite(0xA110_F1A7)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION);

        unsafe {
            let page = allocate_auto_allocation_record_page();
            assert!(
                !page.is_null(),
                "hosted test needs one recovery overflow page"
            );
            (*page).entries[0] = auto_allocation_record_for(ptr, layout, metadata);
            let mut table = AUTO_ALLOCATION_RECORDS[non_home_shard_idx].lock();
            (*page).next = table.overflow;
            table.overflow = page;
        }
        AUTO_ALLOCATION_RECORD_COUNT.store(1, Ordering::Relaxed);
        auto_allocation_record_mark_shard_active(non_home_shard_idx);
        semantic_slow_path_set(SLOW_PATH_ALLOCATION_RECORDS);

        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "overflow pages are only authoritative in the pointer's home shard"
        );
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            None,
            "a corrupt/impossible non-home overflow record must not be consumed as real metadata"
        );

        clear_auto_allocation_records();
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn compiler_auto_allocation_record_mismatch_does_not_remove_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let wrong_layout = Layout::from_size_align(128, align_of::<usize>()).unwrap();
        let ptr = (0x4400usize << 4) as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC002_2401)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C240)
            .with_flags(FLAG_TYPE_ISOLATED);

        unsafe {
            record_auto_allocation_metadata(ptr, layout, metadata);
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptr, wrong_layout),
            None,
            "a bad layout/auth match must not consume the recovery record"
        );
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "the original record must still be recoverable after a mismatch"
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        clear_auto_allocation_records();
    }

    #[test]
    fn compiler_auto_allocation_record_auth_binds_lifetime_hint() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x4500usize << 4) as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC002_2501)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C250)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x17)
            .with_placement_hint(0x29);
        let tampered = metadata.with_lifetime_hint(metadata.lifetime_hint ^ 0x1);
        assert_ne!(
            derive_auto_allocation_record_auth(ptr, layout, metadata),
            derive_auto_allocation_record_auth(ptr, layout, tampered),
            "auto recovery auth must cover lifetime hints, not just type/module/flags/callsite"
        );

        unsafe {
            record_auto_allocation_metadata(ptr, layout, metadata);
        }
        let (record_shard_idx, record_idx) = {
            let (shard_idx, idx) = global_auto_allocation_record_location_for_test(ptr)
                .expect("recorded global auto-allocation metadata");
            let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            table.inline[idx].metadata = tampered;
            (shard_idx, idx)
        };
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "tampered lifetime hints must fail recovery auth without consuming the record"
        );
        {
            let mut table = AUTO_ALLOCATION_RECORDS[record_shard_idx].lock();
            table.inline[record_idx].metadata = metadata;
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "the original record must still be recoverable after an auth mismatch"
        );

        clear_auto_allocation_records();
    }

    #[test]
    fn compiler_auto_allocation_record_auth_includes_process_cookie() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        reset_metadata_auth_cookie_for_test();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x4550usize << 4) as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC002_2550)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C255)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x51)
            .with_placement_hint(0x52);
        let keyed = derive_auto_allocation_record_auth(ptr, layout, metadata);
        let legacy =
            derive_auto_allocation_record_auth_without_cookie_for_test(ptr, layout, metadata);
        assert_ne!(keyed, 0);
        assert_ne!(
            keyed, legacy,
            "compiler recovery auth must include the process cookie, not only public record fields"
        );
        assert_eq!(
            keyed,
            derive_auto_allocation_record_auth(ptr, layout, metadata),
            "the process cookie must be stable for outstanding recovery records"
        );

        unsafe {
            record_auto_allocation_metadata(ptr, layout, metadata);
        }
        let (record_shard_idx, record_idx) = {
            let (shard_idx, idx) = global_auto_allocation_record_location_for_test(ptr)
                .expect("recorded global auto-allocation metadata");
            let mut table = AUTO_ALLOCATION_RECORDS[shard_idx].lock();
            assert_eq!(table.inline[idx].auth, keyed);
            table.inline[idx].auth = legacy;
            (shard_idx, idx)
        };
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "a legacy public-hash authenticator must not spoof a compiler recovery record"
        );
        {
            let mut table = AUTO_ALLOCATION_RECORDS[record_shard_idx].lock();
            table.inline[record_idx].auth = keyed;
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "the keyed recovery record must still be consumable after rejecting the spoofed auth"
        );

        clear_auto_allocation_records();
    }

    #[test]
    fn fast_compiler_auto_allocation_record_auth_binds_placement_hint() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let ptr = (0x4600usize << 4) as *mut u8;
        let metadata = AllocationMetadata::for_type(0xC002_2601)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C260)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x31)
            .with_placement_hint(0x43);
        let tampered = metadata.with_placement_hint(metadata.placement_hint ^ 0x1);
        assert_ne!(
            derive_auto_allocation_record_auth(ptr, layout, metadata),
            derive_auto_allocation_record_auth(ptr, layout, tampered),
            "fast auto recovery auth must cover placement hints as isolation policy"
        );

        with_auto_allocation_recovery_recording(|| unsafe {
            record_auto_allocation_metadata(ptr, layout, metadata);
        });
        unsafe {
            let inline = fast_auto_allocation_record_inline_snapshot_for_test();
            assert_eq!(inline.ptr, ptr as usize);
            assert_ne!(inline.auth, 0);
            FAST_AUTO_ALLOCATION_RECORD_INLINE.metadata = tampered;
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "tampered fast-record placement hints must fail auth without removing the inline record"
        );
        unsafe {
            FAST_AUTO_ALLOCATION_RECORD_INLINE.metadata = metadata;
        }
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            Some(metadata),
            "restoring the protected metadata should make the fast recovery record reusable"
        );

        clear_auto_allocation_records();
    }

    #[test]
    fn compiler_auto_metadata_rejects_empty_type_id_tables() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        let ids = [0xC002_0001_u64];

        assert!(!unsafe {
            semantic_auto_compiler_metadata_enable(0xC0DE, 0, 0xA110_C000, core::ptr::null(), 1)
        });
        assert!(!semantic_auto_metadata_enabled());
        assert!(!unsafe {
            semantic_auto_compiler_metadata_enable(0xC0DE, 0, 0xA110_C000, ids.as_ptr(), 0)
        });
        assert!(!semantic_auto_metadata_enabled());
    }

    #[test]
    fn compiler_auto_metadata_disable_and_reconfigure_wait_for_active_readers() {
        static FIRST_IDS: [u64; 1] = [0xC002_A001];
        static SECOND_IDS: [u64; 1] = [0xC002_A002];

        let _guard = test_guard();
        semantic_auto_metadata_disable();
        assert!(unsafe {
            semantic_auto_compiler_metadata_enable(
                0xC0DE_A001,
                FLAG_TYPE_ISOLATED,
                0xA110_A001,
                FIRST_IDS.as_ptr(),
                FIRST_IDS.len(),
            )
        });

        let reader = AUTO_METADATA_CONFIG.read();
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let reconfigure = thread::spawn(move || {
            started_tx.send(()).unwrap();
            let enabled = unsafe {
                semantic_auto_compiler_metadata_enable(
                    0xC0DE_A002,
                    FLAG_TYPE_ISOLATED,
                    0xA110_A002,
                    SECOND_IDS.as_ptr(),
                    SECOND_IDS.len(),
                )
            };
            done_tx.send(enabled).unwrap();
        });
        started_rx.recv().unwrap();
        assert_eq!(
            done_rx.recv_timeout(std::time::Duration::from_millis(25)),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout),
            "reconfiguration must not return while an allocator reader may still dereference the old compiler-id slice"
        );
        drop(reader);
        assert!(done_rx
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap());
        reconfigure.join().unwrap();
        assert_eq!(
            auto_allocation_metadata(Layout::from_size_align(64, align_of::<usize>()).unwrap())
                .unwrap()
                .type_id,
            SECOND_IDS[0]
        );

        let reader = AUTO_METADATA_CONFIG.read();
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let disable = thread::spawn(move || {
            started_tx.send(()).unwrap();
            semantic_auto_metadata_disable();
            done_tx.send(()).unwrap();
        });
        started_rx.recv().unwrap();
        assert_eq!(
            done_rx.recv_timeout(std::time::Duration::from_millis(25)),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout),
            "disable must wait until every old-slice dereference guard has drained"
        );
        drop(reader);
        done_rx
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();
        disable.join().unwrap();
        assert!(!semantic_auto_metadata_enabled());
        assert_eq!(
            auto_allocation_metadata(Layout::from_size_align(64, align_of::<usize>()).unwrap()),
            None
        );
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn thread_exit_drain_releases_semantic_tls_and_resets_side_state() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        semantic_auto_metadata_disable();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            reset_semantic_scope_stack_for_test();
        }

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let plain = AllocationMetadata::for_type(0xC003_D001)
            .with_module(0xC0DE_D001)
            .with_flags(FLAG_TYPE_ISOLATED);
        let segregated = AllocationMetadata::for_type(0xC004_D001)
            .with_module(0xC0DE_D002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let delayed = AllocationMetadata::for_type(0xC005_D001)
            .with_module(0xC0DE_D003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);

        unsafe {
            let plain_a = alloc.alloc_with_metadata(layout, plain);
            let plain_b = alloc.alloc_with_metadata(layout, plain);
            assert!(!plain_a.is_null() && !plain_b.is_null());
            alloc.dealloc_with_metadata(plain_a, layout, plain);
            alloc.dealloc_with_metadata(plain_b, layout, plain);

            let segregated_a = alloc.alloc_with_metadata(layout, segregated);
            let segregated_b = alloc.alloc_with_metadata(layout, segregated);
            assert!(!segregated_a.is_null() && !segregated_b.is_null());
            alloc.dealloc_with_metadata(segregated_a, layout, segregated);
            alloc.dealloc_with_metadata(segregated_b, layout, segregated);

            let delayed_ptr = alloc.alloc_with_metadata(layout, delayed);
            assert!(!delayed_ptr.is_null());
            alloc.dealloc_with_metadata(delayed_ptr, layout, delayed);

            let recovery_ptr = alloc.alloc_raw(layout);
            assert!(!recovery_ptr.is_null());
            assert!(record_fast_auto_allocation_metadata_eligible(
                recovery_ptr,
                layout,
                plain.with_callsite(0xA110_D001),
            ));
            assert!(current_thread_fast_auto_allocation_records_active());

            let global_recovery_metadata = plain
                .with_callsite(0xA110_D003)
                .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);
            let global_recovery_ptr = alloc.alloc_raw(layout);
            assert!(!global_recovery_ptr.is_null());
            assert!(record_recovery_auto_allocation_metadata(
                global_recovery_ptr,
                layout,
                global_recovery_metadata,
            ));
            assert_eq!(
                lookup_auto_allocation_metadata(global_recovery_ptr, layout),
                Some(global_recovery_metadata)
            );

            #[cfg(not(feature = "fixed_heap"))]
            {
                let huge = AllocationMetadata::for_type(0xC004_D002)
                    .with_module(0xC0DE_D004)
                    .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
                let huge_a = alloc.alloc_with_metadata(layout, huge);
                let huge_b = alloc.alloc_with_metadata(layout, huge);
                let huge_c = alloc.alloc_with_metadata(layout, huge);
                assert!(!huge_a.is_null() && !huge_b.is_null() && !huge_c.is_null());
                alloc.dealloc_with_metadata(huge_a, layout, huge);
                alloc.dealloc_with_metadata(huge_b, layout, huge);
                assert!(hugepage_metadata_side_cache_snapshot().is_none());
                alloc.dealloc_with_metadata(huge_c, layout, huge);
                assert!(hugepage_metadata_side_cache_snapshot().is_some());

                let prot = system_alloc::prots::get_prot(true, true, false);
                let page =
                    system_alloc::mmap(size_of::<MemoryTagPage>(), prot) as *mut MemoryTagPage;
                assert!(!page.is_null() && page as usize != usize::MAX);
                page.write(MemoryTagPage::empty());
                MEMORY_TAG_OVERFLOW = page;
            }

            set_active_metadata(plain.with_callsite(0xA110_D002));
            SEMANTIC_SCOPE_STACK[0] = plain;
            SEMANTIC_SCOPE_STACK_DEPTH = 1;
            AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH = 1;

            assert!(type_isolation_side_cache_snapshot().occupied_entries >= 2);
            assert!(metadata_segregation_side_cache_snapshot().occupied_entries >= 2);
            assert_eq!(delayed_free_snapshot().occupied_slots, 1);

            let released = drain_current_thread_semantic_state(&alloc);
            assert!(
                released >= 8,
                "plain, segregated, delayed, and hugepage allocator-owned TLS objects must all be released"
            );
            assert_eq!(
                type_isolation_side_cache_snapshot(),
                TypeIsolationSideCacheSnapshot {
                    inline_occupied: false,
                    occupied_slots: 0,
                    occupied_entries: 0,
                    retained_bytes: 0,
                    corrupt_slots: 0,
                    hot_slot_active: false,
                }
            );
            assert_eq!(
                metadata_segregation_side_cache_snapshot(),
                MetadataSegregationSideCacheSnapshot {
                    inline_occupied: false,
                    occupied_buckets: 0,
                    occupied_entries: 0,
                    retained_bytes: 0,
                    corrupt_buckets: 0,
                    hot_bucket_active: false,
                }
            );
            assert_eq!(delayed_free_snapshot().occupied_slots, 0);
            assert!(!current_thread_fast_auto_allocation_records_active());
            assert_eq!(
                lookup_auto_allocation_metadata(global_recovery_ptr, layout),
                Some(global_recovery_metadata),
                "thread exit must preserve process-global records for objects allowed to outlive their allocating thread"
            );
            assert_eq!(active_allocation_metadata(), None);
            assert_eq!(semantic_scope_stack_depth_for_test(), 0);
            assert_eq!(
                snapshot_static_copy(core::ptr::addr_of!(
                    AUTO_ALLOCATION_RECOVERY_RECORDING_DEPTH
                )),
                0
            );
            #[cfg(not(feature = "fixed_heap"))]
            {
                assert!(hugepage_metadata_side_cache_snapshot().is_none());
                assert!(MEMORY_TAG_OVERFLOW.is_null());
            }

            // The drain clears only the recovery metadata for caller-owned live
            // objects; this raw allocation remains the caller's responsibility.
            alloc.dealloc_raw(recovery_ptr, layout);
            assert_eq!(
                take_auto_deallocation_metadata(global_recovery_ptr, layout),
                Some(global_recovery_metadata)
            );
            alloc.dealloc_raw(global_recovery_ptr, layout);
        }
    }

    #[cfg(all(not(feature = "fixed_heap"), not(unialloc_target_arm64e)))]
    #[test]
    fn thread_exit_drain_releases_two_inline_hugepage_entries_without_mapping() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        semantic_auto_metadata_disable();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            reset_semantic_scope_stack_for_test();
        }

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC004_D005)
            .with_module(0xC0DE_D005)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null() && !second.is_null());
        assert_ne!(first, second);

        let before = system_alloc::hugepage_mmap_stats_snapshot();
        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            alloc.dealloc_with_metadata(second, layout, metadata);
            let inline = hugepage_inline_segregated_type_cache_entries_snapshot_for_test();
            assert_same_pointer_set([first, second], [inline[0].ptr, inline[1].ptr]);
            assert_eq!(
                inline_segregated_type_cache_occupied_entry_count(
                    SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                ),
                2
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());

            assert_eq!(drain_current_thread_semantic_state(&alloc), 2);
            assert_eq!(
                inline_segregated_type_cache_occupied_entry_count(
                    SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                ),
                0
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
        }
        let after = system_alloc::hugepage_mmap_stats_snapshot();
        assert_eq!(after.attempts, before.attempts);
    }

    #[cfg(not(feature = "stats"))]
    #[test]
    fn stats_feature_disabled_keeps_recording_flags_off() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        semantic_stats_recording_disable();
        semantic_stats_reset();
        semantic_type_stats_recording_enable();

        assert!(!semantic_stats_recording_enabled());
        assert!(!semantic_type_stats_recording_enabled());
        assert!(!semantic_runtime_slow_path_enabled());
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_runtime_slow_path_flags_track_stats_auto_and_scopes() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            restore_active_metadata(AllocationMetadata::unknown());
        }
        assert!(!semantic_runtime_slow_path_enabled());

        semantic_stats_recording_enable();
        assert!(semantic_runtime_slow_path_enabled());
        semantic_stats_recording_disable();
        assert!(!semantic_runtime_slow_path_enabled());

        semantic_auto_metadata_enable(AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED, 0xA11C);
        assert!(semantic_runtime_slow_path_enabled());
        semantic_auto_metadata_disable();
        assert!(!semantic_runtime_slow_path_enabled());

        let outer = AllocationMetadata::for_type(0x5150).with_flags(FLAG_TYPE_ISOLATED);
        let inner = AllocationMetadata::for_type(0x5151).with_flags(FLAG_METADATA_SEGREGATED);
        let prev_outer = unsafe { set_active_metadata(outer) };
        assert_eq!(prev_outer, AllocationMetadata::unknown());
        assert!(semantic_runtime_slow_path_enabled());

        let prev_inner = unsafe { set_active_metadata(inner) };
        assert_eq!(prev_inner, outer);
        assert!(semantic_runtime_slow_path_enabled());

        unsafe {
            restore_active_metadata(prev_inner);
        }
        assert_eq!(active_allocation_metadata(), Some(outer));
        assert!(semantic_runtime_slow_path_enabled());

        unsafe {
            restore_active_metadata(prev_outer);
        }
        assert_eq!(active_allocation_metadata(), None);
        assert!(!semantic_runtime_slow_path_enabled());
    }
    #[cfg(feature = "stats")]
    #[test]
    fn semantic_stats_can_disable_per_type_rows_for_lightweight_probes() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();
        assert!(semantic_stats_recording_enabled());
        assert!(semantic_type_stats_recording_enabled());

        semantic_type_stats_recording_disable();
        assert!(semantic_stats_recording_enabled());
        assert!(!semantic_type_stats_recording_enabled());
        assert!(
            semantic_runtime_slow_path_enabled(),
            "aggregate stats alone still need the semantic slow path"
        );

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_5A71)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5A71)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);

        let mut rows = [empty_type_stats_row(); 4];
        let row_count = semantic_type_stats_snapshot(&mut rows);
        assert_eq!(
            row_count, 0,
            "lightweight stats mode should not lock/populate per-type rows"
        );

        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
        semantic_type_stats_recording_enable();
        assert!(semantic_type_stats_recording_enabled());

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }
        let row_count = semantic_type_stats_snapshot(&mut rows);
        assert!(row_count >= 1);
        let row = rows
            .iter()
            .take(row_count)
            .find(|row| row.type_id == metadata.type_id)
            .expect("per-type stats row after re-enable");
        assert_eq!(row.allocations, 1);
        assert_eq!(row.deallocations, 1);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn semantic_runtime_slow_path_sticky_scope_gate_preserves_other_flags() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            restore_active_metadata(AllocationMetadata::unknown());
            clear_scoped_metadata_gate_for_test();
        }
        assert!(!semantic_runtime_slow_path_enabled());

        scoped_metadata_activate();
        scoped_metadata_activate();
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "a sticky global scope gate should not slow unrelated threads without local scoped metadata"
        );

        semantic_stats_recording_enable();
        assert!(semantic_stats_recording_enabled());
        assert!(semantic_runtime_slow_path_enabled());

        scoped_metadata_deactivate();
        assert!(semantic_runtime_slow_path_enabled());
        scoped_metadata_deactivate();
        assert!(semantic_runtime_slow_path_enabled());
        assert!(semantic_stats_recording_enabled());

        semantic_stats_recording_disable();
        assert!(!semantic_stats_recording_enabled());
        assert!(!semantic_runtime_slow_path_enabled());
        unsafe {
            clear_scoped_metadata_gate_for_test();
        }
    }

    #[test]
    fn active_metadata_updates_global_scope_gate_without_counting_nested_replacements() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            clear_scoped_metadata_gate_for_test();
        }
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            0
        );

        let first = AllocationMetadata::for_type(0xC002_7201)
            .with_module(0x5343_4F50)
            .with_callsite(0xA110_7201)
            .with_flags(FLAG_TYPE_ISOLATED);
        let second = first.with_callsite(0xA110_7202);
        let previous = unsafe { set_active_metadata(first) };
        assert_eq!(previous, AllocationMetadata::unknown());
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT,
            "first active scope should arm the global scoped slow-path gate"
        );
        let first_saved = unsafe { set_active_metadata(second) };
        assert_eq!(first_saved, first);
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT,
            "active-to-active replacement should not double-count the same thread"
        );

        unsafe {
            restore_active_metadata(first_saved);
        }
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT
        );
        unsafe {
            restore_active_metadata(previous);
        }
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT,
            "restoring the unknown base scope leaves the sticky global gate armed"
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "inactive local metadata must disable the scoped slow path even while the sticky gate is armed"
        );
        unsafe {
            clear_scoped_metadata_gate_for_test();
        }
    }

    #[test]
    fn compiler_scope_push_pop_updates_global_gate_once_per_thread() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            reset_semantic_scope_stack_for_test();
            clear_scoped_metadata_gate_for_test();
        }

        __unialloc_semantic_scope_push(0xC002_7202, 0x5343_4F50, FLAG_TYPE_ISOLATED, 0xA110_7202);
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT
        );
        __unialloc_semantic_scope_push(0xC002_7203, 0x5343_4F50, FLAG_TYPE_ISOLATED, 0xA110_7203);
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT,
            "nested compiler scopes should not make unrelated threads pay multiple scoped counts"
        );
        __unialloc_semantic_scope_pop();
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT
        );
        __unialloc_semantic_scope_pop();
        assert_eq!(
            SEMANTIC_SLOW_PATH_FLAGS.load(Ordering::Relaxed) & SLOW_PATH_SCOPED_METADATA_MASK,
            SLOW_PATH_SCOPED_METADATA_UNIT
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "popping the last compiler scope clears local activity even while the global gate remains armed"
        );
        unsafe {
            clear_scoped_metadata_gate_for_test();
        }
    }

    #[test]
    fn fast_recovery_records_update_global_runtime_gate() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
            clear_scoped_metadata_gate_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_7204)
            .with_module(0x5343_4F50)
            .with_callsite(0xA110_7204)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert_eq!(
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed),
            0
        );
        assert!(!semantic_runtime_slow_path_enabled());

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed),
            1,
            "same-thread compiler recovery record should arm the runtime gate"
        );
        assert!(semantic_runtime_slow_path_enabled());

        unsafe {
            alloc.dealloc(ptr, layout);
        }
        assert_eq!(
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.load(Ordering::Relaxed),
            0,
            "consuming the fast recovery record should disarm the gate"
        );

        unsafe {
            let cached = pop_semantic_type_cache(layout, metadata).expect("typed free cached");
            alloc.dealloc_raw(cached, layout);
            clear_auto_allocation_records();
        }
    }

    #[test]
    fn fast_recovery_record_insert_stale_hot_slot_skips_duplicate_probe() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();

            let layout =
                Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_7205)
                .with_module(0x5343_4F50)
                .with_callsite(0xA110_7205)
                .with_flags(FLAG_TYPE_ISOLATED);
            let other_metadata = AllocationMetadata::for_type(0xC003_7206)
                .with_module(0x5343_4F50)
                .with_callsite(0xA110_7206)
                .with_flags(FLAG_TYPE_ISOLATED);
            let ptr = 0x4100_0000usize as *mut u8;
            let other_ptr = 0x4200_0000usize as *mut u8;
            let inline_ptr = 0x4300_0000usize as *mut u8;
            let ptr_key = ptr as usize;
            let start = fast_auto_allocation_record_slot(ptr);

            FAST_AUTO_ALLOCATION_RECORD_INLINE =
                fast_auto_allocation_record_for(inline_ptr, layout, other_metadata);
            FAST_AUTO_ALLOCATION_RECORDS[start] =
                fast_auto_allocation_record_for(other_ptr, layout, other_metadata);
            FAST_AUTO_ALLOCATION_RECORD_COUNT = 1;
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.store(2, Ordering::Relaxed);
            remember_fast_auto_allocation_record_hot_slot(ptr_key, start);

            FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS.store(0, Ordering::Relaxed);
            assert!(record_fast_auto_allocation_metadata_eligible(
                ptr, layout, metadata
            ));
            assert_eq!(
                FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS.load(Ordering::Relaxed),
                2,
                "stale hot slot should be inspected once, then skipped when offset=0 reaches the same slot"
            );
            assert_ne!(
                FAST_AUTO_ALLOCATION_RECORDS[start].ptr, ptr_key,
                "the unrelated stale record in the primary slot must not be overwritten"
            );
            assert_eq!(
                recover_fast_auto_allocation_record_metadata(ptr, layout, true),
                Some(metadata),
                "new record should be recoverable from the next available probe slot"
            );

            clear_auto_allocation_records();
        }
    }

    #[test]
    fn fast_recovery_record_lookup_stale_hot_slot_skips_duplicate_probe() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();

            let layout =
                Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_7207)
                .with_module(0x5343_4F50)
                .with_callsite(0xA110_7207)
                .with_flags(FLAG_TYPE_ISOLATED);
            let other_metadata = AllocationMetadata::for_type(0xC003_7208)
                .with_module(0x5343_4F50)
                .with_callsite(0xA110_7208)
                .with_flags(FLAG_TYPE_ISOLATED);
            let ptr = 0x4400_0000usize as *mut u8;
            let other_ptr = 0x4500_0000usize as *mut u8;
            let ptr_key = ptr as usize;
            let start = fast_auto_allocation_record_slot(ptr);
            let next = (start + 1) & (FAST_AUTO_ALLOCATION_RECORD_SLOTS - 1);

            FAST_AUTO_ALLOCATION_RECORDS[start] =
                fast_auto_allocation_record_for(other_ptr, layout, other_metadata);
            FAST_AUTO_ALLOCATION_RECORDS[next] =
                fast_auto_allocation_record_for(ptr, layout, metadata);
            FAST_AUTO_ALLOCATION_RECORD_COUNT = 2;
            FAST_AUTO_ALLOCATION_RECORD_GLOBAL_COUNT.store(2, Ordering::Relaxed);
            remember_fast_auto_allocation_record_hot_slot(ptr_key, start);

            FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS.store(0, Ordering::Relaxed);
            assert_eq!(
                recover_fast_auto_allocation_record_metadata(ptr, layout, false),
                Some(metadata)
            );
            assert_eq!(
                FAST_AUTO_ALLOCATION_RECORD_PROBE_STEPS.load(Ordering::Relaxed),
                2,
                "lookup should inspect the stale hot slot once and then jump to the next real probe slot"
            );
            assert_eq!(
                fast_auto_allocation_record_hot_slot_snapshot_for_test().slot_idx,
                next,
                "successful lookup should replace the stale hot hint with the real slot"
            );

            clear_auto_allocation_records();
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn stats_recording_does_not_force_same_thread_recovery_into_global_table() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
            clear_scoped_metadata_gate_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5771)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5771)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "aggregate/per-type stats should not force same-thread recovery records through the global mutex table"
        );
        assert!(
            current_thread_fast_auto_allocation_records_active(),
            "same-thread conservative compiler ABI should keep recovery metadata in TLS fast records"
        );
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), Some(metadata));

        unsafe {
            RustAllocator::new().dealloc(ptr, layout);
        }
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "same-thread free should not touch the global recovery table"
        );

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);

        unsafe {
            let cached = pop_semantic_type_cache(layout, metadata).expect("typed free cached");
            RustAllocator::new().dealloc_raw(cached, layout);
            clear_auto_allocation_records();
        }
    }

    #[test]
    fn semantic_type_cache_class_keeps_small_side_table_objects_cacheable() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let plain_layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE.saturating_sub(1).max(1), 1)
                .unwrap();
        let side_table_layout = Layout::from_size_align(1, 1).unwrap();
        let plain = AllocationMetadata::for_type(0xC003_5101).with_flags(FLAG_TYPE_ISOLATED);
        let side_table = AllocationMetadata::for_type(0xC003_5102)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        assert_eq!(semantic_type_cache_class(plain_layout, plain), None);
        assert_eq!(
            semantic_type_cache_class(side_table_layout, side_table),
            Some(SemanticTypeCacheClass::Segregated),
            "side-table metadata cacheability must not inherit the plain-cache minimum object size"
        );

        let alloc = RustAllocator::new();
        let ptr = unsafe { alloc.alloc_with_metadata(side_table_layout, side_table) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, side_table_layout, side_table);
            let reused = alloc.alloc_with_metadata(side_table_layout, side_table);
            assert_eq!(
                reused, ptr,
                "small metadata-segregated objects should still reuse through the semantic side cache"
            );
            alloc.dealloc_raw(reused, side_table_layout);
        }
    }

    #[test]
    fn semantic_type_cache_class_rejects_oversized_objects() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let capped_layout =
            Layout::from_size_align(MAX_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let oversized_layout = Layout::from_size_align(
            MAX_TYPE_CACHE_OBJECT_SIZE + size_of::<usize>(),
            align_of::<usize>(),
        )
        .unwrap();
        let plain = AllocationMetadata::for_type(0xC003_5103).with_flags(FLAG_TYPE_ISOLATED);
        let side_table = AllocationMetadata::for_type(0xC003_5104)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let hugepage = AllocationMetadata::for_type(0xC003_5105)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        assert_eq!(
            semantic_type_cache_class(capped_layout, plain),
            Some(SemanticTypeCacheClass::Plain),
            "the cap is inclusive so ordinary medium objects still use the plain semantic cache"
        );
        assert_eq!(semantic_type_cache_class(oversized_layout, plain), None);
        assert_eq!(
            semantic_type_cache_class(oversized_layout, side_table),
            None
        );
        assert_eq!(semantic_type_cache_class(oversized_layout, hugepage), None);
    }

    #[test]
    fn compiler_type_metadata_cache_class_matches_fast_path_policy_surface() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let tiny_layout = Layout::from_size_align(1, 1).unwrap();
        let plain_layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let oversized_layout = Layout::from_size_align(
            MAX_TYPE_CACHE_OBJECT_SIZE + size_of::<usize>(),
            align_of::<usize>(),
        )
        .unwrap();
        let plain = AllocationMetadata::for_type(0xC003_5106).with_flags(FLAG_TYPE_ISOLATED);
        let metadata_segregated = AllocationMetadata::for_type(0xC003_5107)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let hugepage = AllocationMetadata::for_type(0xC003_5108)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let protected = AllocationMetadata::for_type(0xC003_5109)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION);
        let delayed = AllocationMetadata::for_type(0xC003_5110)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);

        for (layout, metadata) in [
            (tiny_layout, plain),
            (plain_layout, plain),
            (tiny_layout, metadata_segregated),
            (tiny_layout, hugepage),
            (tiny_layout, protected),
            (oversized_layout, plain),
            (oversized_layout, metadata_segregated),
        ] {
            assert!(
                compiler_type_isolated_recovery_fast_path(metadata),
                "test case should stay inside the compiler fast-path flag surface"
            );
            assert_eq!(
                compiler_type_metadata_cache_class(layout, metadata),
                semantic_type_cache_class(layout, metadata),
                "compiler fast-path cache classifier must be a shortcut, not a different policy"
            );
        }

        assert!(
            !compiler_type_isolated_recovery_fast_path(delayed),
            "delayed-free metadata must keep using the full semantic path"
        );
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_cache_bypasses_oversized_objects_without_retaining_them() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(
            MAX_TYPE_CACHE_OBJECT_SIZE + size_of::<usize>(),
            align_of::<usize>(),
        )
        .unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5106).with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert_eq!(
                pop_semantic_type_cache(layout, metadata),
                None,
                "oversized typed frees must not be retained in the semantic cache"
            );
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(
            snap.typed_cache_inserts, 0,
            "oversized typed objects should bypass semantic cache insertion"
        );
        assert!(
            snap.typed_cache_bypasses >= 1,
            "oversized typed object free/pop should be counted as cache bypass"
        );
        semantic_stats_recording_disable();
    }

    #[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
    #[test]
    fn semantic_type_cache_slot_byte_budget_bypasses_excess_real_free() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        const OBJECTS: usize = 6;
        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MAX_TYPE_CACHE_SLOT_BYTES / 4, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5107).with_flags(FLAG_TYPE_ISOLATED);
        let mut ptrs = [core::ptr::null_mut(); OBJECTS];

        for ptr in ptrs.iter_mut() {
            *ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!ptr.is_null());
        }
        for &ptr in ptrs.iter() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
        }

        let after_free = type_isolation_side_cache_snapshot();
        assert!(
            after_free.inline_occupied,
            "the first real typed free should still use the inline hot slot"
        );
        assert_eq!(
            after_free.occupied_entries, 5,
            "one inline object plus four 16KiB linked entries fit the 64KiB cold-slot budget"
        );

        let mut popped = 0usize;
        while let Some(ptr) = unsafe { pop_semantic_type_cache(layout, metadata) } {
            popped += 1;
            unsafe {
                alloc.dealloc_raw(ptr, layout);
            }
        }
        assert_eq!(
            popped, 5,
            "the sixth real free should bypass semantic retention and return to the allocator"
        );

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, 5);
        assert!(
            snap.typed_cache_bypasses >= 1,
            "the over-budget free should be counted as a cache bypass"
        );
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn semantic_type_cache_aggregate_budget_bypasses_excess_real_frees() {
        let _guard = test_guard();
        let mut cached_metadata = Vec::new();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MAX_TYPE_CACHE_SLOT_BYTES / 4, align_of::<usize>()).unwrap();
        let retained = type_cache_retained_bytes_for_layout(layout);
        assert!(retained > 0);

        let mut next_type = 0xC003_5310u64;
        let mut attempts = 0usize;
        while type_isolation_side_cache_snapshot()
            .retained_bytes
            .saturating_add(retained)
            <= MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES
        {
            assert!(
                attempts < 16_384,
                "real allocator traffic should find enough insertable semantic cache keys"
            );
            let metadata = AllocationMetadata::for_type(next_type).with_flags(FLAG_TYPE_ISOLATED);
            next_type = next_type.wrapping_add(1);
            attempts += 1;

            let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!ptr.is_null());
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
            cached_metadata.push(metadata);
        }

        let before_overflow = type_isolation_side_cache_snapshot();
        assert!(
            before_overflow.retained_bytes <= MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES
                && before_overflow.retained_bytes.saturating_add(retained)
                    > MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES
        );

        let overflow_metadata =
            AllocationMetadata::for_type(next_type).with_flags(FLAG_TYPE_ISOLATED);
        let overflow_ptr = unsafe { alloc.alloc_with_metadata(layout, overflow_metadata) };
        assert!(!overflow_ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(overflow_ptr, layout, overflow_metadata);
        }

        let after_overflow = type_isolation_side_cache_snapshot();
        if after_overflow.retained_bytes != before_overflow.retained_bytes {
            unsafe {
                while let Some(ptr) = pop_semantic_type_cache(layout, overflow_metadata) {
                    alloc.dealloc_raw(ptr, layout);
                }
            }
            panic!(
                "over-budget real typed free should bypass semantic cache retention: before={} after={}",
                before_overflow.retained_bytes, after_overflow.retained_bytes
            );
        }

        for metadata in cached_metadata {
            unsafe {
                while let Some(ptr) = pop_semantic_type_cache(layout, metadata) {
                    alloc.dealloc_raw(ptr, layout);
                }
            }
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn metadata_segregated_type_cache_aggregate_budget_bypasses_excess_real_frees() {
        let _guard = test_guard();
        let mut cached_metadata = Vec::new();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(
            MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES / 4,
            align_of::<usize>(),
        )
        .unwrap();
        let retained = type_cache_retained_bytes_for_layout(layout);
        assert!(retained > 0);

        let mut next_type = 0xC003_5410u64;
        let mut attempts = 0usize;
        while metadata_segregation_side_cache_snapshot()
            .retained_bytes
            .saturating_add(retained)
            <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
        {
            assert!(
                attempts < 16_384,
                "real allocator traffic should find enough insertable segregated cache keys"
            );
            let metadata = AllocationMetadata::for_type(next_type)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            next_type = next_type.wrapping_add(1);
            attempts += 1;
            unsafe {
                if !INLINE_SEGREGATED_TYPE_CACHE_ENTRY.is_empty()
                    && !metadata_segregated_growth_bucket_available_for_test(layout, metadata)
                {
                    continue;
                }
            }

            let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!ptr.is_null());
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
            cached_metadata.push(metadata);
        }

        let before_overflow = metadata_segregation_side_cache_snapshot();
        assert!(
            before_overflow.retained_bytes <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
                && before_overflow.retained_bytes.saturating_add(retained)
                    > MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
        );

        let overflow_metadata = loop {
            let metadata = AllocationMetadata::for_type(next_type)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            next_type = next_type.wrapping_add(1);
            unsafe {
                if metadata_segregated_growth_bucket_available_for_test(layout, metadata) {
                    break metadata;
                }
            }
        };
        let overflow_ptr = unsafe { alloc.alloc_with_metadata(layout, overflow_metadata) };
        assert!(!overflow_ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(overflow_ptr, layout, overflow_metadata);
        }

        let after_overflow = metadata_segregation_side_cache_snapshot();
        if after_overflow.retained_bytes != before_overflow.retained_bytes {
            unsafe {
                while let Some(ptr) = pop_semantic_type_cache(layout, overflow_metadata) {
                    alloc.dealloc_raw(ptr, layout);
                }
            }
            panic!(
                "over-budget real metadata-segregated free should bypass side-cache retention: before={} after={}",
                before_overflow.retained_bytes, after_overflow.retained_bytes
            );
        }

        for metadata in cached_metadata {
            unsafe {
                while let Some(ptr) = pop_semantic_type_cache(layout, metadata) {
                    alloc.dealloc_raw(ptr, layout);
                }
            }
        }
    }

    #[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
    #[test]
    fn segregated_type_cache_bucket_byte_budget_bypasses_excess_real_free() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        const OBJECTS: usize = 6;
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(
            MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES / 4,
            align_of::<usize>(),
        )
        .unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5108)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let mut ptrs = [core::ptr::null_mut(); OBJECTS];

        for ptr in ptrs.iter_mut() {
            *ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!ptr.is_null());
        }
        for &ptr in ptrs.iter() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
        }

        let after_free = metadata_segregation_side_cache_snapshot();
        assert!(
            !after_free.inline_occupied,
            "the matching inline entry should have materialized into the bucket before the cap is hit"
        );
        assert_eq!(after_free.occupied_buckets, 1);
        assert_eq!(
            after_free.occupied_entries, 4,
            "four 16KiB side-table entries fill the 64KiB bucket budget"
        );

        let mut popped = 0usize;
        while let Some(ptr) = unsafe { pop_semantic_type_cache(layout, metadata) } {
            popped += 1;
            unsafe {
                alloc.dealloc_raw(ptr, layout);
            }
        }
        assert_eq!(
            popped, 4,
            "later real frees should bypass side-cache retention once the bucket byte cap is full"
        );

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, 4);
        assert!(
            snap.typed_cache_bypasses >= 1,
            "over-budget metadata-segregated frees should be counted as cache bypasses"
        );
        semantic_stats_recording_disable();
    }

    #[test]
    fn scoped_metadata_slow_path_is_thread_local_without_global_modes() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            restore_active_metadata(AllocationMetadata::unknown());
            clear_scoped_metadata_gate_for_test();
        }
        assert!(!semantic_runtime_slow_path_enabled());
        assert!(!semantic_allocation_slow_path_enabled());

        let metadata = AllocationMetadata::for_type(0xC002_7101)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_7101)
            .with_flags(FLAG_TYPE_ISOLATED);
        let previous = unsafe { set_active_metadata(metadata) };
        assert!(semantic_runtime_slow_path_enabled());
        assert!(semantic_allocation_slow_path_enabled());

        let worker = thread::spawn(|| {
            assert_eq!(active_allocation_metadata(), None);
            assert!(
                !semantic_runtime_slow_path_enabled(),
                "another thread should not pay semantic slow-path overhead only because this thread has scoped metadata"
            );
            assert!(!semantic_allocation_slow_path_enabled());
        });
        worker.join().expect("worker slow-path check");

        unsafe {
            restore_active_metadata(previous);
        }
        assert_eq!(active_allocation_metadata(), None);
        assert!(!semantic_runtime_slow_path_enabled());
        assert!(!semantic_allocation_slow_path_enabled());
    }

    #[test]
    fn local_scoped_global_alloc_avoids_recovery_record() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();
        unsafe {
            reset_semantic_scope_stack_for_test();
            clear_scoped_metadata_gate_for_test();
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let base_metadata = AllocationMetadata::for_type(0xC002_7205)
            .with_module(0x5343_4F50)
            .with_callsite(0xA110_7205)
            .with_flags(FLAG_TYPE_ISOLATED);

        __unialloc_semantic_scope_push_local(
            base_metadata.type_id,
            base_metadata.module_id,
            base_metadata.flags,
            base_metadata.callsite,
        );
        let local_metadata = active_allocation_metadata().expect("local scope metadata");
        assert!(
            !active_allocation_metadata_requires_recovery_record(local_metadata),
            "local scope should select exact metadata without pointer recovery"
        );

        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "compiler-local scoped GlobalAlloc must not create a recovery record"
        );
        assert_eq!(
            take_auto_deallocation_metadata(ptr, layout),
            None,
            "no recovery record should be available before paired local Drop scope"
        );

        unsafe {
            alloc.dealloc(ptr, layout);
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "paired local deallocation should leave no recovery side-table state"
        );

        __unialloc_semantic_scope_pop();
        assert_eq!(active_allocation_metadata(), None);
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, local_metadata);
            reset_semantic_scope_stack_for_test();
            clear_scoped_metadata_gate_for_test();
        }
    }

    #[test]
    fn layout_auto_metadata_bypasses_exact_type_cache() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();

        semantic_auto_metadata_enable(AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED, 0xA11C);
        let metadata = auto_allocation_metadata(layout).expect("auto metadata enabled");
        semantic_auto_metadata_disable();

        assert!(metadata.has_type());
        assert!(metadata.is_layout_derived());
        assert!(!type_cache_eligible(layout, metadata));
        assert!(type_cache_eligible(
            layout,
            AllocationMetadata::for_type(metadata.type_id).with_flags(FLAG_TYPE_ISOLATED)
        ));
    }
    #[cfg(feature = "stats")]
    #[test]
    fn layout_derived_raw_fast_path_only_skips_non_effective_exact_policies() {
        let _guard = test_guard();
        semantic_stats_recording_disable();
        let layout_metadata = AllocationMetadata {
            type_id: semantic_layout_id(64, align_of::<usize>()),
            module_id: AUTO_LAYOUT_MODULE_ID,
            flags: FLAG_TYPE_ISOLATED
                | FLAG_METADATA_SEGREGATED
                | FLAG_HUGEPAGE_METADATA
                | FLAG_POINTER_AUTH
                | FLAG_METADATA_PROTECTION,
            lifetime_hint: 0,
            placement_hint: LAYOUT_DERIVED_PLACEMENT_HINT,
            callsite: 0xA11C,
        };

        assert!(layout_derived_raw_only_fast_path(layout_metadata));
        assert!(!layout_derived_raw_only_fast_path(
            layout_metadata.with_flags(layout_metadata.flags | FLAG_FORCE_INITIALIZE)
        ));
        assert!(!layout_derived_raw_only_fast_path(
            layout_metadata.with_flags(layout_metadata.flags | FLAG_DELAYED_FREE)
        ));
        assert!(!layout_derived_raw_only_fast_path(
            layout_metadata.with_flags(layout_metadata.flags | FLAG_GUARD_PAGES)
        ));
        assert!(!layout_derived_raw_only_fast_path(
            layout_metadata.with_flags(layout_metadata.flags | FLAG_MEMORY_TAGGING)
        ));

        semantic_stats_recording_enable();
        assert!(!layout_derived_raw_only_fast_path(layout_metadata));
        semantic_stats_recording_disable();
    }

    #[test]
    fn metadata_segregated_type_cache_preserves_object_header() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_0001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let sentinels = [
            0x1111_2222_3333_4444usize,
            0x5555_6666_7777_8888usize,
            0x9999_AAAA_BBBB_CCCCusize,
        ];

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            let words = ptr as *mut usize;
            for (idx, value) in sentinels.iter().copied().enumerate() {
                words.add(idx).write(value);
            }
            alloc.dealloc_with_metadata(ptr, layout, metadata);

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            let words = reused as *mut usize;
            for (idx, value) in sentinels.iter().copied().enumerate() {
                assert_eq!(words.add(idx).read(), value);
            }
            alloc.dealloc_raw(reused, layout);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn metadata_segregated_type_cache_reuses_from_side_table() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_0002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);

            let snap = semantic_stats_snapshot();
            assert_eq!(snap.typed_cache_inserts, 1);
            assert_eq!(snap.typed_cache_hits, 1);
            assert!(snap.typed_cache_bypasses >= 1);

            semantic_stats_recording_disable();
            alloc.dealloc_raw(reused, layout);
        }
    }

    #[test]
    fn metadata_segregated_type_cache_hot_bucket_tracks_repeated_key() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_0004)
            .with_module(0xC003)
            .with_callsite(0xC004)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert!(!second.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                ptr,
                "single-entry metadata-segregated frees should first use the inline TLS side-cache"
            );
            alloc.dealloc_with_metadata(second, layout, metadata);
            let hot_hint = segregated_type_cache_hot_bucket_snapshot_for_test();
            assert!(hot_hint.matches(layout, metadata));
            let hot_idx = hot_hint.bucket_idx;
            assert!(hot_idx < TYPE_CACHE_SLOTS);
            let hot_bucket = segregated_type_cache_bucket_snapshot_for_test(hot_idx);
            let hot_entry = hot_bucket
                .entries
                .iter()
                .take(hot_bucket.count)
                .find(|entry| entry.ptr == ptr)
                .expect("cached metadata-segregated entry");
            assert_eq!(
                hot_entry.policy_key,
                segregated_type_cache_policy_key(metadata),
                "hot side-cache entry should carry a precomputed policy key"
            );

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            let hot_hint = segregated_type_cache_hot_bucket_snapshot_for_test();
            assert!(hot_hint.matches(layout, metadata));
            assert_eq!(hot_hint.bucket_idx, hot_idx);

            let other_policy = metadata.with_flags(
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED | FLAG_METADATA_PROTECTION,
            );
            assert!(
                !segregated_type_cache_hot_bucket_snapshot_for_test().matches(layout, other_policy),
                "hot bucket hint must include integrity/segregation policy bits"
            );

            alloc.dealloc_raw(reused, layout);
            drain_semantic_cache_for_test(&alloc, layout, metadata);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn metadata_segregated_hot_bucket_pop_miss_does_not_rescan_start_bucket() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let layout =
                Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_0041)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let start = segregated_type_cache_slot(metadata, layout);
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                start,
            );

            SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.store(0, Ordering::Relaxed);
            assert!(pop_segregated_type_cache_from(
                ordinary_segregated_type_cache_mut(),
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
            )
            .is_none());
            assert_eq!(
                SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.load(Ordering::Relaxed),
                SEGREGATED_TYPE_CACHE_PROBE_LIMIT,
                "a hot bucket equal to the primary probe bucket should be inspected once, not once via hot hint plus again via offset=0"
            );
        }
    }

    #[test]
    fn metadata_segregated_hot_bucket_push_search_skips_checked_full_bucket() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let layout =
                Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_0042)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let start = segregated_type_cache_slot(metadata, layout);
            let mut stale_storage =
                [[0usize; TYPE_CACHE_NODE_WORDS * 2]; SEGREGATED_TYPE_CACHE_DEPTH];
            let mut fresh_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let mut inline_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let other_metadata = AllocationMetadata::for_type(0x5E6D_0043)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let other_cache_key = type_cache_identity_key(other_metadata);
            let other_policy_key = segregated_type_cache_policy_key(other_metadata);

            for idx in 0..SEGREGATED_TYPE_CACHE_DEPTH {
                let ptr = stale_storage[idx].as_mut_ptr() as *mut u8;
                SEGREGATED_TYPE_CACHE[start].entries[idx] = SegregatedTypeCacheEntry {
                    cache_key: other_cache_key,
                    type_id: other_metadata.type_id,
                    policy_key: other_policy_key,
                    ptr,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(ptr, layout, other_metadata),
                    metadata: other_metadata,
                };
            }
            SEGREGATED_TYPE_CACHE[start].count = SEGREGATED_TYPE_CACHE_DEPTH;
            SEGREGATED_TYPE_CACHE[start].retained_bytes =
                layout.size() * SEGREGATED_TYPE_CACHE_DEPTH;
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                start,
            );
            let inline_ptr = inline_storage.as_mut_ptr() as *mut u8;
            INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry {
                cache_key: other_cache_key,
                type_id: other_metadata.type_id,
                policy_key: other_policy_key,
                ptr: inline_ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(inline_ptr, layout, other_metadata),
                metadata: other_metadata,
            };

            SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.store(0, Ordering::Relaxed);
            let fresh = fresh_storage.as_mut_ptr() as *mut u8;
            assert!(push_segregated_type_cache_eligible_with_key(
                fresh, layout, metadata, cache_key, policy_key,
            )
            .expect("push should find the next empty bucket")
            .is_none());
            assert_eq!(
                SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.load(Ordering::Relaxed),
                2,
                "push should inspect the hot full bucket once, then the next empty probe bucket"
            );
            assert_eq!(
                pop_segregated_type_cache(layout, metadata),
                Some(fresh),
                "fresh entry must remain reusable from the bucket selected after the skipped hot slot"
            );
        }
    }

    #[test]
    fn metadata_segregated_push_skips_byte_capped_hot_bucket() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let layout =
                Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_0044)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let start = segregated_type_cache_slot(metadata, layout);
            let next_bucket = (start + 1) & (TYPE_CACHE_SLOTS - 1);

            let other_metadata = AllocationMetadata::for_type(0x5E6D_0045)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let other_cache_key = type_cache_identity_key(other_metadata);
            let other_policy_key = segregated_type_cache_policy_key(other_metadata);
            let capped_size = MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES - layout.size() + 1;
            let capped_layout = Layout::from_size_align(capped_size, align_of::<usize>()).unwrap();
            let mut capped_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let mut fresh_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let mut inline_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];

            let capped_ptr = capped_storage.as_mut_ptr() as *mut u8;
            SEGREGATED_TYPE_CACHE[start].entries[0] = SegregatedTypeCacheEntry {
                cache_key: other_cache_key,
                type_id: other_metadata.type_id,
                policy_key: other_policy_key,
                ptr: capped_ptr,
                size: capped_size,
                align: capped_layout.align(),
                auth: metadata_record_auth(capped_ptr, capped_layout, other_metadata),
                metadata: other_metadata,
            };
            SEGREGATED_TYPE_CACHE[start].count = 1;
            SEGREGATED_TYPE_CACHE[start].retained_bytes = capped_size;
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                start,
            );

            let inline_ptr = inline_storage.as_mut_ptr() as *mut u8;
            INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry {
                cache_key: other_cache_key,
                type_id: other_metadata.type_id,
                policy_key: other_policy_key,
                ptr: inline_ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(inline_ptr, layout, other_metadata),
                metadata: other_metadata,
            };

            SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.store(0, Ordering::Relaxed);
            let fresh = fresh_storage.as_mut_ptr() as *mut u8;
            assert!(push_segregated_type_cache_eligible_with_key(
                fresh, layout, metadata, cache_key, policy_key,
            )
            .expect("push should keep searching after a byte-capped hot bucket")
            .is_none());

            assert_eq!(
                SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.load(Ordering::Relaxed),
                2,
                "push should inspect the byte-capped hot bucket once, then the next eligible bucket"
            );
            assert_eq!(
                SEGREGATED_TYPE_CACHE[next_bucket].entries[0].ptr, fresh,
                "fresh entry should use the next bucket instead of bypassing at the byte cap"
            );
            assert_eq!(
                segregated_type_cache_hot_bucket_snapshot_for_test().bucket_idx,
                next_bucket,
                "the hot hint should move to the bucket that actually accepted the entry"
            );
            assert_eq!(
                pop_segregated_type_cache(layout, metadata),
                Some(fresh),
                "byte-cap fallback bucket entry must be immediately reusable"
            );
            assert_eq!(
                SEGREGATED_TYPE_CACHE[start].entries[0].ptr, capped_ptr,
                "the capped unrelated bucket should remain intact"
            );
        }
    }

    #[test]
    fn metadata_segregated_bucket_push_reuses_single_aggregate_scan() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let layout =
                Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_0046)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let start = segregated_type_cache_slot(metadata, layout);
            let mut stale_storage =
                [[0usize; TYPE_CACHE_NODE_WORDS * 2]; SEGREGATED_TYPE_CACHE_DEPTH];
            let mut fresh_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let mut second_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let mut inline_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let other_metadata = AllocationMetadata::for_type(0x5E6D_0047)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let other_cache_key = type_cache_identity_key(other_metadata);
            let other_policy_key = segregated_type_cache_policy_key(other_metadata);

            for idx in 0..SEGREGATED_TYPE_CACHE_DEPTH {
                let ptr = stale_storage[idx].as_mut_ptr() as *mut u8;
                SEGREGATED_TYPE_CACHE[start].entries[idx] = SegregatedTypeCacheEntry {
                    cache_key: other_cache_key,
                    type_id: other_metadata.type_id,
                    policy_key: other_policy_key,
                    ptr,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(ptr, layout, other_metadata),
                    metadata: other_metadata,
                };
            }
            SEGREGATED_TYPE_CACHE[start].count = SEGREGATED_TYPE_CACHE_DEPTH;
            SEGREGATED_TYPE_CACHE[start].retained_bytes =
                layout.size() * SEGREGATED_TYPE_CACHE_DEPTH;
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                start,
            );

            // Keep the inline slot occupied so this test exercises the bucket
            // path, where a single free used to rescan the entire TLS table for
            // each candidate/final aggregate-budget check.
            let inline_ptr = inline_storage.as_mut_ptr() as *mut u8;
            INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry {
                cache_key: other_cache_key,
                type_id: other_metadata.type_id,
                policy_key: other_policy_key,
                ptr: inline_ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(inline_ptr, layout, other_metadata),
                metadata: other_metadata,
            };

            SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.store(0, Ordering::Relaxed);
            let fresh = fresh_storage.as_mut_ptr() as *mut u8;
            assert!(push_segregated_type_cache_eligible_with_key(
                fresh, layout, metadata, cache_key, policy_key,
            )
            .expect("push should find the next empty bucket")
            .is_none());
            assert_eq!(
                SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.load(Ordering::Relaxed),
                1,
                "bucket insertion should reuse one validated aggregate retained-byte snapshot"
            );
            let second = second_storage.as_mut_ptr() as *mut u8;
            assert!(push_segregated_type_cache_eligible_with_key(
                second, layout, metadata, cache_key, policy_key,
            )
            .expect("second push should reuse the trusted aggregate counter")
            .is_none());
            assert_eq!(
                SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.load(Ordering::Relaxed),
                1,
                "later bucket insertions should use the trusted retained-byte counter instead of rescanning"
            );
            let reused_a = pop_segregated_type_cache(layout, metadata)
                .expect("first optimized bucket entry should remain reusable");
            let reused_b = pop_segregated_type_cache(layout, metadata)
                .expect("second optimized bucket entry should remain reusable");
            assert!(
                (reused_a == fresh && reused_b == second)
                    || (reused_a == second && reused_b == fresh),
                "the optimized budget check must not change reuse behavior"
            );
        }
    }

    #[test]
    fn metadata_segregated_inline_push_skips_empty_bucket_scans() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let layout =
                Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_0048)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let ptr = storage.as_mut_ptr() as *mut u8;

            SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.store(0, Ordering::Relaxed);
            SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.store(0, Ordering::Relaxed);
            assert!(push_segregated_type_cache_eligible_with_key(
                ptr, layout, metadata, cache_key, policy_key,
            )
            .expect("empty bucket table should accept the inline hot entry")
            .is_none());

            assert_eq!(
                SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.load(Ordering::Relaxed),
                0,
                "empty materialized bucket tables should not be probed before inline insertion"
            );
            assert_eq!(
                SEGREGATED_TYPE_CACHE_AGGREGATE_SCANS.load(Ordering::Relaxed),
                0,
                "empty materialized bucket tables should not need a retained-byte table scan"
            );
            assert_eq!(
                metadata_segregation_side_cache_snapshot().retained_bytes,
                type_cache_retained_bytes_for_layout(layout),
                "the O(1) inline path must keep footprint accounting visible"
            );
            assert_eq!(
                pop_segregated_type_cache(layout, metadata),
                Some(ptr),
                "the O(1) inline path must preserve immediate reuse semantics"
            );
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn metadata_segregated_type_cache_keeps_multiple_entries_per_bucket() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_2001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let mut cached = [core::ptr::null_mut(); SEGREGATED_TYPE_CACHE_DEPTH];

        for slot in cached.iter_mut() {
            *slot = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!(*slot).is_null());
        }
        for ptr in cached.iter().copied() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
        }

        unsafe {
            let bucket = &SEGREGATED_TYPE_CACHE[segregated_type_cache_slot(metadata, layout)];
            assert_eq!(bucket.count, SEGREGATED_TYPE_CACHE_DEPTH);
        }

        let mut reused = [core::ptr::null_mut(); SEGREGATED_TYPE_CACHE_DEPTH];
        for idx in 0..reused.len() {
            reused[idx] = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(cached.iter().any(|candidate| *candidate == reused[idx]));
            assert_eq!(
                reused
                    .iter()
                    .take(idx + 1)
                    .filter(|candidate| **candidate == reused[idx])
                    .count(),
                1
            );
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, SEGREGATED_TYPE_CACHE_DEPTH);
        assert_eq!(snap.typed_cache_hits, SEGREGATED_TYPE_CACHE_DEPTH);

        semantic_stats_recording_disable();
        for ptr in reused {
            unsafe {
                alloc.dealloc_raw(ptr, layout);
            }
        }
    }

    #[test]
    fn metadata_segregated_type_cache_does_not_reuse_across_modules() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let module_a = AllocationMetadata::for_type(0x5E6D_2101)
                .with_module(0xC0DE_A)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let module_b = AllocationMetadata::for_type(module_a.type_id)
                .with_module(0xC0DE_B)
                .with_flags(module_a.flags);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_segregated_type_cache(ptr, layout, module_a)
                .expect("cache insert")
                .is_none());
            assert_eq!(
                pop_segregated_type_cache(layout, module_b),
                None,
                "segregated metadata cache must not alias same type id across modules"
            );
            assert_eq!(pop_segregated_type_cache(layout, module_a), Some(ptr));
        }
    }

    #[test]
    fn metadata_segregated_type_cache_clears_corrupt_count_before_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_CA01)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let bucket_idx = segregated_type_cache_slot(metadata, layout);
            let ptr = storage.as_mut_ptr() as *mut u8;

            SEGREGATED_TYPE_CACHE[bucket_idx].entries[0] = SegregatedTypeCacheEntry {
                cache_key,
                type_id: metadata.type_id,
                policy_key,
                ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(ptr, layout, metadata),
                metadata,
            };
            SEGREGATED_TYPE_CACHE[bucket_idx].count = SEGREGATED_TYPE_CACHE_DEPTH + 1;
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                bucket_idx,
            );

            assert_eq!(pop_segregated_type_cache(layout, metadata), None);
            assert_eq!(SEGREGATED_TYPE_CACHE[bucket_idx].count, 0);
            assert!(SEGREGATED_TYPE_CACHE[bucket_idx]
                .entries
                .iter()
                .all(|entry| entry.is_empty()));
            assert_eq!(
                segregated_type_cache_hot_bucket_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn metadata_segregated_type_cache_clears_active_hole_before_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_CA03)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let bucket_idx = segregated_type_cache_slot(metadata, layout);
            let ptr = storage.as_mut_ptr() as *mut u8;

            SEGREGATED_TYPE_CACHE[bucket_idx].entries[1] = SegregatedTypeCacheEntry {
                cache_key,
                type_id: metadata.type_id,
                policy_key,
                ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(ptr, layout, metadata),
                metadata,
            };
            SEGREGATED_TYPE_CACHE[bucket_idx].count = 2;
            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                bucket_idx,
            );

            let before = metadata_segregation_side_cache_snapshot();
            assert_eq!(before.corrupt_buckets, 1);
            assert_eq!(
                before.occupied_buckets, 1,
                "corrupt buckets should remain visible as occupied diagnostics"
            );
            assert_eq!(
                before.occupied_entries, 1,
                "the snapshot should count observable entries in corrupt metadata buckets"
            );
            assert_eq!(
                before.retained_bytes, 0,
                "corrupt bucket byte accounting is untrusted and excluded"
            );

            assert_eq!(pop_segregated_type_cache(layout, metadata), None);
            assert_eq!(SEGREGATED_TYPE_CACHE[bucket_idx].count, 0);
            assert!(SEGREGATED_TYPE_CACHE[bucket_idx]
                .entries
                .iter()
                .all(|entry| entry.is_empty()));
            assert_eq!(
                segregated_type_cache_hot_bucket_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn metadata_segregated_type_cache_recovers_corrupt_count_on_push() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_CA02)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let bucket_idx = segregated_type_cache_slot(metadata, layout);
            let ptr = storage.as_mut_ptr() as *mut u8;

            SEGREGATED_TYPE_CACHE[bucket_idx].count = SEGREGATED_TYPE_CACHE_DEPTH + 1;
            assert!(push_segregated_type_cache(ptr, layout, metadata)
                .expect("corrupt bucket should be reusable after clearing")
                .is_none());

            let bucket = &SEGREGATED_TYPE_CACHE[bucket_idx];
            assert_eq!(bucket.count, 1);
            assert_eq!(bucket.entries[0].ptr, ptr);
            assert_eq!(bucket.entries[0].type_id, metadata.type_id);
            assert_eq!(pop_segregated_type_cache(layout, metadata), Some(ptr));
        }
    }

    #[test]
    fn metadata_segregated_type_cache_recovers_stale_inactive_entry_on_push() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let mut stale_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_CA04)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let bucket_idx = segregated_type_cache_slot(metadata, layout);
            let stale_ptr = stale_storage.as_mut_ptr() as *mut u8;
            let ptr = storage.as_mut_ptr() as *mut u8;

            SEGREGATED_TYPE_CACHE[bucket_idx].entries[1] = SegregatedTypeCacheEntry {
                cache_key,
                type_id: metadata.type_id,
                policy_key,
                ptr: stale_ptr,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(stale_ptr, layout, metadata),
                metadata,
            };
            SEGREGATED_TYPE_CACHE[bucket_idx].count = 0;
            assert_eq!(
                metadata_segregation_side_cache_snapshot().corrupt_buckets,
                1
            );

            assert!(push_segregated_type_cache(ptr, layout, metadata)
                .expect("stale inactive bucket should be reusable after clearing")
                .is_none());

            let bucket = &SEGREGATED_TYPE_CACHE[bucket_idx];
            assert_eq!(bucket.count, 1);
            assert_eq!(bucket.entries[0].ptr, ptr);
            assert!(bucket.entries[1..].iter().all(|entry| entry.is_empty()));
            assert_eq!(pop_segregated_type_cache(layout, metadata), Some(ptr));
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn metadata_segregated_type_cache_probes_neighbor_bucket_for_collision() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let base = AllocationMetadata::for_type(0x5E6D_4001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let primary = segregated_type_cache_slot(base, layout);
        let collider = (1..4096u64)
            .map(|delta| {
                AllocationMetadata::for_type(base.type_id.wrapping_add(delta))
                    .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED)
            })
            .find(|metadata| segregated_type_cache_slot(*metadata, layout) == primary)
            .expect("bounded search should find same-bucket type id");
        assert_ne!(collider.type_id, base.type_id);

        let mut cached = [core::ptr::null_mut(); SEGREGATED_TYPE_CACHE_DEPTH];
        for slot in cached.iter_mut() {
            *slot = unsafe { alloc.alloc_with_metadata(layout, base) };
            assert!(!(*slot).is_null());
        }
        for ptr in cached.iter().copied() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, base);
            }
        }

        let colliding_ptr = unsafe { alloc.alloc_with_metadata(layout, collider) };
        let colliding_second = unsafe { alloc.alloc_with_metadata(layout, collider) };
        assert!(!colliding_ptr.is_null());
        assert!(!colliding_second.is_null());
        unsafe {
            alloc.dealloc_with_metadata(colliding_ptr, layout, collider);
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                colliding_ptr,
                "the first colliding free should remain a one-entry inline hit"
            );
            alloc.dealloc_with_metadata(colliding_second, layout, collider);

            assert_eq!(
                SEGREGATED_TYPE_CACHE[primary].count,
                SEGREGATED_TYPE_CACHE_DEPTH
            );
            let mut found_bucket = None;
            let mut offset = 0;
            while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
                let idx = (primary + offset) & (TYPE_CACHE_SLOTS - 1);
                let bucket = &SEGREGATED_TYPE_CACHE[idx];
                if bucket
                    .entries
                    .iter()
                    .take(bucket.count)
                    .any(|entry| entry.ptr == colliding_ptr)
                {
                    found_bucket = Some(idx);
                    break;
                }
                offset += 1;
            }
            let found_bucket = found_bucket.expect("colliding entry should stay in probe window");
            assert_ne!(found_bucket, primary);
        }

        let reused_colliding = unsafe { alloc.alloc_with_metadata(layout, collider) };
        assert_eq!(reused_colliding, colliding_ptr);
        unsafe {
            alloc.dealloc_raw(reused_colliding, layout);
        }

        let mut reused = [core::ptr::null_mut(); SEGREGATED_TYPE_CACHE_DEPTH];
        for idx in 0..reused.len() {
            reused[idx] = unsafe { alloc.alloc_with_metadata(layout, base) };
            assert!(cached.iter().any(|candidate| *candidate == reused[idx]));
            assert_eq!(
                reused
                    .iter()
                    .take(idx + 1)
                    .filter(|candidate| **candidate == reused[idx])
                    .count(),
                1
            );
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, SEGREGATED_TYPE_CACHE_DEPTH + 2);
        assert_eq!(snap.typed_cache_hits, SEGREGATED_TYPE_CACHE_DEPTH + 1);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, collider);
        }
        for ptr in reused {
            unsafe {
                alloc.dealloc_raw(ptr, layout);
            }
        }
    }

    #[test]
    fn metadata_segregated_type_cache_eviction_stays_with_matching_collision_bucket() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let base = AllocationMetadata::for_type(0x5E6D_5001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let primary = segregated_type_cache_slot(base, layout);
        let mut colliding = [base; SEGREGATED_TYPE_CACHE_PROBE_LIMIT];
        let mut found = 1;
        let mut delta = 1;
        while found < SEGREGATED_TYPE_CACHE_PROBE_LIMIT && delta < 65536 {
            let metadata = AllocationMetadata::for_type(base.type_id.wrapping_add(delta))
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            if segregated_type_cache_slot(metadata, layout) == primary {
                colliding[found] = metadata;
                found += 1;
            }
            delta += 1;
        }
        assert_eq!(found, SEGREGATED_TYPE_CACHE_PROBE_LIMIT);

        for metadata in colliding.iter().copied() {
            let mut cached = [core::ptr::null_mut(); SEGREGATED_TYPE_CACHE_DEPTH];
            for ptr in cached.iter_mut() {
                *ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
                assert!(!(*ptr).is_null());
            }
            for ptr in cached.iter().copied() {
                unsafe {
                    alloc.dealloc_with_metadata(ptr, layout, metadata);
                }
            }
        }

        let extra = unsafe { alloc.alloc_with_metadata(layout, colliding[1]) };
        assert!(!extra.is_null());
        unsafe {
            alloc.dealloc_with_metadata(extra, layout, colliding[1]);

            let primary_bucket = &SEGREGATED_TYPE_CACHE[primary];
            assert_eq!(primary_bucket.count, SEGREGATED_TYPE_CACHE_DEPTH);
            assert!(primary_bucket
                .entries
                .iter()
                .take(primary_bucket.count)
                .all(|entry| entry.type_id == base.type_id));

            let matching_bucket_idx = (primary + 1) & (TYPE_CACHE_SLOTS - 1);
            let matching_bucket = &SEGREGATED_TYPE_CACHE[matching_bucket_idx];
            assert_eq!(matching_bucket.count, SEGREGATED_TYPE_CACHE_DEPTH);
            assert!(matching_bucket
                .entries
                .iter()
                .take(matching_bucket.count)
                .all(|entry| entry.type_id == colliding[1].type_id));
            assert!(matching_bucket
                .entries
                .iter()
                .take(matching_bucket.count)
                .any(|entry| entry.ptr == extra));

            drain_segregated_probe_window_for_test(&alloc, primary);
        }

        semantic_stats_recording_disable();
    }

    #[test]
    fn metadata_segregated_full_bucket_replacement_does_not_shift_entries() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_5101)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let slot = segregated_type_cache_slot(metadata, layout);
        let mut cached = [core::ptr::null_mut(); SEGREGATED_TYPE_CACHE_DEPTH];

        for ptr in cached.iter_mut() {
            *ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!(*ptr).is_null());
        }
        for ptr in cached.iter().copied() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
        }

        unsafe {
            let bucket = &SEGREGATED_TYPE_CACHE[slot];
            assert_eq!(bucket.count, SEGREGATED_TYPE_CACHE_DEPTH);
            assert_eq!(bucket.evict_cursor, 0);
            let before = bucket.entries.map(|entry| entry.ptr);

            let extra = alloc.alloc_raw(layout);
            assert!(!extra.is_null());
            let cache_key = type_cache_identity_key(metadata);
            let evicted = SEGREGATED_TYPE_CACHE[slot]
                .push(SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key: segregated_type_cache_policy_key(metadata),
                    ptr: extra,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(extra, layout, metadata),
                    metadata,
                })
                .expect("full side-cache bucket should evict exactly one entry");

            let bucket = &SEGREGATED_TYPE_CACHE[slot];
            let after = bucket.entries.map(|entry| entry.ptr);
            assert_eq!(evicted.ptr, before[0]);
            assert_eq!(after[0], extra);
            assert_eq!(
                &after[1..],
                &before[1..],
                "full-bucket replacement should not shift unrelated entries"
            );
            assert_eq!(bucket.evict_cursor, 1);

            release_evicted_segregated_type_cache_entry(&alloc, evicted);
            drain_segregated_probe_window_for_test(&alloc, slot);
        }

        semantic_stats_recording_disable();
    }

    #[test]
    fn segregated_type_cache_skips_incompatible_integrity_policy() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let base = AllocationMetadata::for_type(0x5E6D_3001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let pac = AllocationMetadata::for_type(base.type_id)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED | FLAG_POINTER_AUTH);

        let cached = unsafe { alloc.alloc_with_metadata(layout, base) };
        assert!(!cached.is_null());
        unsafe {
            alloc.dealloc_with_metadata(cached, layout, base);

            let fresh = alloc.alloc_with_metadata(layout, pac);
            assert!(!fresh.is_null());
            assert_ne!(fresh, cached);
            alloc.dealloc_raw(fresh, layout);

            let reused = alloc.alloc_with_metadata(layout, base);
            assert_eq!(reused, cached);
            alloc.dealloc_raw(reused, layout);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn metadata_segregated_inline_side_cache_serves_single_hot_entry() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_5201)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let slot = segregated_type_cache_slot(metadata, layout);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());

        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                ptr,
                "first hot metadata-segregated free should use the O(1) TLS side-cache"
            );
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().policy_key,
                segregated_type_cache_policy_key(metadata)
            );
            assert_eq!(
                SEGREGATED_TYPE_CACHE[slot].count, 0,
                "the single hot entry should avoid the bucket side-cache"
            );

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            assert!(INLINE_SEGREGATED_TYPE_CACHE_ENTRY.is_empty());
            alloc.dealloc_raw(reused, layout);
        }

        let snap = semantic_stats_snapshot();
        if semantic_stats_feature_enabled() {
            assert_eq!(snap.typed_cache_inserts, 1);
            assert_eq!(snap.typed_cache_hits, 1);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn metadata_segregated_two_object_pair_stays_inline_without_bucket_probes() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let first_metadata = AllocationMetadata::for_type(0x5E6D_5211)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let second_metadata = AllocationMetadata::for_type(0x5E6D_5212)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let first = unsafe { alloc.alloc_with_metadata(layout, first_metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, second_metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert_ne!(first, second);
        let probes_before_free = SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.load(Ordering::Relaxed);

        unsafe {
            alloc.dealloc_with_metadata(first, layout, first_metadata);
            alloc.dealloc_with_metadata(second, layout, second_metadata);
        }

        let cached = metadata_segregation_side_cache_snapshot();
        assert!(cached.inline_occupied);
        assert_eq!(cached.occupied_entries, 2);
        assert_eq!(
            cached.retained_bytes,
            2 * type_cache_retained_bytes_for_layout(layout)
        );
        assert_eq!(
            cached.occupied_buckets, 0,
            "a two-object semantic pair should not materialize the bucket table"
        );
        assert_eq!(
            SEGREGATED_TYPE_CACHE_BUCKET_PROBE_STEPS.load(Ordering::Relaxed),
            probes_before_free,
            "freeing a two-object semantic pair should stay on the O(1) inline path"
        );

        unsafe {
            let reused_first = alloc.alloc_with_metadata(layout, first_metadata);
            let reused_second = alloc.alloc_with_metadata(layout, second_metadata);
            assert_eq!(reused_first, first);
            assert_eq!(reused_second, second);
            alloc.dealloc_raw(reused_first, layout);
            alloc.dealloc_raw(reused_second, layout);
        }
    }

    #[test]
    fn metadata_segregated_side_cache_snapshot_tracks_inline_entry() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let initial = metadata_segregation_side_cache_snapshot();
        assert!(!initial.inline_occupied);
        assert_eq!(initial.occupied_entries, 0);
        assert_eq!(initial.corrupt_buckets, 0);

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_5207)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let after_free = metadata_segregation_side_cache_snapshot();
        assert!(
            after_free.inline_occupied,
            "single hot metadata-segregated free should be visible in the snapshot"
        );
        assert_eq!(
            after_free.occupied_entries, 1,
            "the inline hot slot is a retained side-cache entry"
        );
        assert_eq!(
            after_free.occupied_buckets, 0,
            "the inline hot slot must not be counted as a materialized bucket"
        );
        assert_eq!(
            after_free.retained_bytes,
            type_cache_retained_bytes_for_layout(layout),
            "inline metadata-segregated snapshots should expose allocator-rounded retained bytes"
        );
        assert_eq!(after_free.corrupt_buckets, 0);

        let reused = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_eq!(reused, ptr);
        let after_reuse = metadata_segregation_side_cache_snapshot();
        assert!(!after_reuse.inline_occupied);
        assert_eq!(after_reuse.occupied_entries, 0);
        unsafe {
            alloc.dealloc_raw(reused, layout);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn side_cache_snapshots_report_plain_retained_bytes() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5181).with_flags(FLAG_TYPE_ISOLATED);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert_ne!(first, second);

        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            alloc.dealloc_with_metadata(second, layout, metadata);
        }

        let after_free = type_isolation_side_cache_snapshot();
        assert!(after_free.inline_occupied);
        assert_eq!(after_free.occupied_slots, 1);
        assert_eq!(after_free.occupied_entries, 2);
        assert_eq!(
            after_free.retained_bytes,
            type_cache_retained_bytes_for_layout(layout) * 2,
            "snapshot should expose allocator-rounded bytes retained by inline+linked plain semantic cache"
        );

        let reused_a = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let reused_b = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(reused_a == first || reused_a == second);
        assert!(reused_b == first || reused_b == second);
        assert_ne!(reused_a, reused_b);
        assert_eq!(type_isolation_side_cache_snapshot().retained_bytes, 0);
        unsafe {
            alloc.dealloc_raw(reused_a, layout);
            alloc.dealloc_raw(reused_b, layout);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn plain_type_cache_repairs_retained_byte_counter_before_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5183).with_flags(FLAG_TYPE_ISOLATED);
        let cache_key = type_cache_identity_key(metadata);
        let slot_key = plain_type_cache_slot_key(cache_key, layout);

        let inline = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let linked = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!inline.is_null());
        assert!(!linked.is_null());
        assert_ne!(inline, linked);

        unsafe {
            alloc.dealloc_with_metadata(inline, layout, metadata);
            alloc.dealloc_with_metadata(linked, layout, metadata);

            let mut slot_idx = None;
            let mut offset = 0usize;
            let start = type_cache_slot(slot_key);
            while offset < TYPE_CACHE_PROBE_LIMIT {
                let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
                if TYPE_CACHE[idx].cache_key == slot_key {
                    slot_idx = Some(idx);
                    break;
                }
                offset += 1;
            }
            let slot_idx = slot_idx.expect("linked plain cache entry should occupy a probe slot");
            assert_eq!(TYPE_CACHE[slot_idx].count, 1);
            assert_eq!(
                TYPE_CACHE[slot_idx].retained_bytes,
                type_cache_retained_bytes_for_layout(layout)
            );
            TYPE_CACHE[slot_idx].retained_bytes = 0;

            let reused_inline = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused_inline, inline);
            let reused_linked = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(
                reused_linked, linked,
                "accounting-only drift must be repaired instead of dropping the owned linked cache entry"
            );

            alloc.dealloc_raw(reused_inline, layout);
            alloc.dealloc_raw(reused_linked, layout);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn metadata_segregated_type_cache_repairs_retained_byte_counter_before_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5184)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let bucket_idx = segregated_type_cache_slot(metadata, layout);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert_ne!(first, second);

        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            alloc.dealloc_with_metadata(second, layout, metadata);

            assert_eq!(SEGREGATED_TYPE_CACHE[bucket_idx].count, 2);
            assert_eq!(
                SEGREGATED_TYPE_CACHE[bucket_idx].retained_bytes,
                type_cache_retained_bytes_for_layout(layout) * 2
            );
            SEGREGATED_TYPE_CACHE[bucket_idx].retained_bytes = 0;

            let reused_a = alloc.alloc_with_metadata(layout, metadata);
            assert!(reused_a == first || reused_a == second);
            let reused_b = alloc.alloc_with_metadata(layout, metadata);
            assert!(reused_b == first || reused_b == second);
            assert_ne!(reused_a, reused_b);

            alloc.dealloc_raw(reused_a, layout);
            alloc.dealloc_raw(reused_b, layout);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn side_cache_snapshots_report_metadata_segregated_retained_bytes() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5182)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert_ne!(first, second);

        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            alloc.dealloc_with_metadata(second, layout, metadata);
        }

        let after_free = metadata_segregation_side_cache_snapshot();
        assert!(
            !after_free.inline_occupied,
            "a matching second free materializes the inline entry into the bucket"
        );
        assert_eq!(after_free.occupied_buckets, 1);
        assert_eq!(after_free.occupied_entries, 2);
        assert_eq!(
            after_free.retained_bytes,
            type_cache_retained_bytes_for_layout(layout) * 2,
            "snapshot should expose allocator-rounded bytes retained by metadata side-cache buckets"
        );

        let reused_a = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let reused_b = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(reused_a == first || reused_a == second);
        assert!(reused_b == first || reused_b == second);
        assert_ne!(reused_a, reused_b);
        assert_eq!(metadata_segregation_side_cache_snapshot().retained_bytes, 0);
        unsafe {
            alloc.dealloc_raw(reused_a, layout);
            alloc.dealloc_raw(reused_b, layout);
            clear_type_cache_for_test();
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_uses_inline_side_cache_before_bucket_materialization() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_5202)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null() && !second.is_null() && !third.is_null());
        assert_ne!(first, second);
        assert_ne!(first, third);
        assert_ne!(second, third);
        let before = system_alloc::hugepage_mmap_stats_snapshot();

        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            assert_eq!(
                hugepage_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                first,
                "a single hugepage metadata free should use the O(1) hugepage inline side-cache"
            );
            assert!(
                hugepage_second_inline_segregated_type_cache_entry_snapshot_for_test().is_empty()
            );
            assert!(
                INLINE_SEGREGATED_TYPE_CACHE_ENTRY.is_empty(),
                "hugepage inline caching must not occupy the ordinary metadata inline slot"
            );
            assert_eq!(
                hugepage_segregated_bucket_count_for_test(layout, metadata),
                None,
                "one cached hugepage metadata object should not allocate the mmap_huge bucket table"
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());

            alloc.dealloc_with_metadata(second, layout, metadata);
            let inline = hugepage_inline_segregated_type_cache_entries_snapshot_for_test();
            assert_same_pointer_set([first, second], [inline[0].ptr, inline[1].ptr]);
            assert_eq!(
                inline_segregated_type_cache_occupied_entry_count(
                    SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                ),
                2
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
            assert_eq!(
                system_alloc::hugepage_mmap_stats_snapshot().attempts,
                before.attempts,
                "the two inline entries must not materialize the hugepage bucket table"
            );

            alloc.dealloc_with_metadata(third, layout, metadata);
            assert_eq!(
                hugepage_segregated_bucket_count_for_test(layout, metadata),
                Some(3),
                "the third matching object should materialize all three entries into one bucket"
            );
            let snapshot = hugepage_metadata_side_cache_snapshot()
                .expect("the third matching object should materialize side-cache backing");
            assert_eq!(snapshot.occupied_buckets, 1);
            assert_eq!(snapshot.occupied_entries, 3);
            assert!(
                system_alloc::hugepage_mmap_stats_snapshot().attempts > before.attempts,
                "the 2→3 boundary should attempt hugepage side-cache mapping"
            );

            let reused_one = alloc.alloc_with_metadata(layout, metadata);
            let reused_two = alloc.alloc_with_metadata(layout, metadata);
            let reused_three = alloc.alloc_with_metadata(layout, metadata);
            assert_same_pointer_set(
                [first, second, third],
                [reused_one, reused_two, reused_three],
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
            alloc.dealloc_raw(reused_one, layout);
            alloc.dealloc_raw(reused_two, layout);
            alloc.dealloc_raw(reused_three, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }

        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_inline_side_cache_does_not_contend_with_ordinary_metadata_inline() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let ordinary = AllocationMetadata::for_type(0x5E6D_5301)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let hugepage = AllocationMetadata::for_type(0x5E6D_5302)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        let ordinary_ptr = unsafe { alloc.alloc_with_metadata(layout, ordinary) };
        let hugepage_ptr = unsafe { alloc.alloc_with_metadata(layout, hugepage) };
        let hugepage_ptr_second = unsafe { alloc.alloc_with_metadata(layout, hugepage) };
        assert!(!ordinary_ptr.is_null());
        assert!(!hugepage_ptr.is_null());
        assert!(!hugepage_ptr_second.is_null());
        assert_ne!(ordinary_ptr, hugepage_ptr);
        assert_ne!(hugepage_ptr, hugepage_ptr_second);

        unsafe {
            alloc.dealloc_with_metadata(ordinary_ptr, layout, ordinary);
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                ordinary_ptr,
                "ordinary metadata should keep its one-object hot entry"
            );
            assert_eq!(
                inline_segregated_type_cache_occupied_entry_count(
                    SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                ),
                0,
                "ordinary metadata must not occupy either hugepage inline slot"
            );

            alloc.dealloc_with_metadata(hugepage_ptr, layout, hugepage);
            alloc.dealloc_with_metadata(hugepage_ptr_second, layout, hugepage);
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                ordinary_ptr,
                "hugepage frees must not evict the ordinary inline hot entry"
            );
            let hugepage_inline = hugepage_inline_segregated_type_cache_entries_snapshot_for_test();
            assert_same_pointer_set(
                [hugepage_ptr, hugepage_ptr_second],
                [hugepage_inline[0].ptr, hugepage_inline[1].ptr],
            );
            assert_eq!(
                hugepage_segregated_bucket_count_for_test(layout, hugepage),
                None,
                "two hugepage hot entries should not materialize the mmap_huge bucket table"
            );
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "two hugepage hot entries should not allocate snapshot-visible side-cache backing"
            );

            let reused_hugepage_one = alloc.alloc_with_metadata(layout, hugepage);
            let reused_hugepage_two = alloc.alloc_with_metadata(layout, hugepage);
            assert_same_pointer_set(
                [hugepage_ptr, hugepage_ptr_second],
                [reused_hugepage_one, reused_hugepage_two],
            );
            assert_eq!(
                inline_segregated_type_cache_occupied_entry_count(
                    SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                ),
                0
            );
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                ordinary_ptr,
                "reusing hugepage metadata must leave the ordinary inline entry intact"
            );

            let reused_ordinary = alloc.alloc_with_metadata(layout, ordinary);
            assert_eq!(reused_ordinary, ordinary_ptr);
            assert!(INLINE_SEGREGATED_TYPE_CACHE_ENTRY.is_empty());

            alloc.dealloc_raw(reused_hugepage_one, layout);
            alloc.dealloc_raw(reused_hugepage_two, layout);
            alloc.dealloc_raw(reused_ordinary, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }

        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn hugepage_metadata_reuses_single_inline_entry() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_0003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let sentinels = [
            0xA111_B222_C333_D444usize,
            0xE555_F666_A777_B888usize,
            0xC999_DAAA_EBBB_FCCCusize,
        ];

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            let words = ptr as *mut usize;
            for (idx, value) in sentinels.iter().copied().enumerate() {
                words.add(idx).write(value);
            }
            alloc.dealloc_with_metadata(ptr, layout, metadata);

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            let words = reused as *mut usize;
            for (idx, value) in sentinels.iter().copied().enumerate() {
                assert_eq!(words.add(idx).read(), value);
            }
            alloc.dealloc_raw(reused, layout);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, 1);
        assert_eq!(snap.typed_cache_hits, 1);
        assert!(snap.policy_flags_seen & FLAG_HUGEPAGE_METADATA != 0);
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_single_free_does_not_allocate_empty_side_cache() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        let after_alloc = system_alloc::hugepage_mmap_stats_snapshot();
        assert_eq!(
            unsafe { hugepage_segregated_bucket_count_for_test(layout, metadata) },
            None,
            "cold allocation should leave the hugepage side cache unallocated until free caches an object"
        );

        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }
        let after_dealloc = system_alloc::hugepage_mmap_stats_snapshot();
        assert_eq!(
            after_dealloc.attempts, after_alloc.attempts,
            "freeing one hugepage metadata object should stay in the inline side cache"
        );
        assert!(hugepage_metadata_side_cache_snapshot().is_none());
        assert_eq!(
            unsafe { hugepage_segregated_bucket_count_for_test(layout, metadata) },
            None
        );

        let reused = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_eq!(reused, ptr);
        unsafe {
            alloc.dealloc_raw(reused, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_two_object_reuse_does_not_repeat_mmap_attempts() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6005)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let mut first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let mut second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert_ne!(first, second);

        let before = system_alloc::hugepage_mmap_stats_snapshot();
        for _ in 0..4 {
            unsafe {
                alloc.dealloc_with_metadata(first, layout, metadata);
                alloc.dealloc_with_metadata(second, layout, metadata);
                let inline = hugepage_inline_segregated_type_cache_entries_snapshot_for_test();
                assert_same_pointer_set([first, second], [inline[0].ptr, inline[1].ptr]);
                assert_eq!(
                    inline_segregated_type_cache_occupied_entry_count(
                        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                    ),
                    2
                );
                assert!(hugepage_metadata_side_cache_snapshot().is_none());
                assert_eq!(
                    system_alloc::hugepage_mmap_stats_snapshot().attempts,
                    before.attempts,
                    "two-object caching must remain entirely inline"
                );
            }

            let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert_eq!(
                unsafe {
                    inline_segregated_type_cache_occupied_entry_count(
                        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                    )
                },
                1
            );
            let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert_same_pointer_set([first, second], [reused_one, reused_two]);
            assert_eq!(
                unsafe {
                    inline_segregated_type_cache_occupied_entry_count(
                        SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                    )
                },
                0
            );
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "two-object hot reuse should not leave an mmap-backed side cache"
            );
            first = reused_one;
            second = reused_two;
        }
        let after = system_alloc::hugepage_mmap_stats_snapshot();
        assert_eq!(
            after.attempts, before.attempts,
            "bounded two-object reuse must not materialize and tear down the hugepage side cache on every round"
        );

        unsafe {
            alloc.dealloc_raw(first, layout);
            alloc.dealloc_raw(second, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_fallback_aggregate_budget_counts_both_inline_entries() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MAX_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let retained = type_cache_retained_bytes_for_layout(layout);
        let metadata = AllocationMetadata::for_type(0x5E6D_6006)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let ordinary_metadata = AllocationMetadata::for_type(0x5E6D_6007)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let first = unsafe { alloc.alloc_raw(layout) };
        let second = unsafe { alloc.alloc_raw(layout) };
        let ordinary = unsafe { alloc.alloc_raw(layout) };
        assert!(!first.is_null() && !second.is_null() && !ordinary.is_null());
        assert_ne!(first, second);
        assert_ne!(first, ordinary);
        assert_ne!(second, ordinary);

        unsafe {
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry {
                cache_key: type_cache_identity_key(ordinary_metadata),
                type_id: ordinary_metadata.type_id,
                policy_key: segregated_type_cache_policy_key(ordinary_metadata),
                ptr: ordinary,
                size: layout.size(),
                align: layout.align(),
                auth: metadata_record_auth(ordinary, layout, ordinary_metadata),
                metadata: ordinary_metadata,
            };
            *inline_segregated_type_cache_entry_at(SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE, 0) =
                SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key,
                    ptr: first,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(first, layout, metadata),
                    metadata,
                };
            *inline_segregated_type_cache_entry_at(SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE, 1) =
                SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key,
                    ptr: second,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(second, layout, metadata),
                    metadata,
                };
            set_segregated_type_cache_bucket_retained_bytes(
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES.saturating_sub(retained * 3),
                true,
            );

            let bucket_idx = segregated_type_cache_slot_for_key(cache_key, layout);
            let cache = ordinary_segregated_type_cache_mut();
            let mut ordinary_cached = None;
            assert!(
                segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
                    &*cache,
                    SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                    SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                    &mut ordinary_cached,
                    bucket_idx,
                    layout.size(),
                ),
                "the mocked ordinary budget has room when only its own inline ownership is counted"
            );

            let mut hugepage_cached = None;
            assert!(
                !segregated_type_cache_can_accept_aggregate_with_cached_retained_bytes(
                    &*cache,
                    SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                    SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                    &mut hugepage_cached,
                    bucket_idx,
                    layout.size(),
                ),
                "ordinary fallback must count ordinary inline plus both hugepage inline objects before accepting a third"
            );
            assert_eq!(
                hugepage_cached,
                Some(MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES)
            );

            *inline_segregated_type_cache_entry_at(SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE, 0) =
                SegregatedTypeCacheEntry::empty();
            *inline_segregated_type_cache_entry_at(SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE, 1) =
                SegregatedTypeCacheEntry::empty();
            INLINE_SEGREGATED_TYPE_CACHE_ENTRY = SegregatedTypeCacheEntry::empty();
            set_segregated_type_cache_bucket_retained_bytes(
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                0,
                false,
            );
            alloc.dealloc_raw(first, layout);
            alloc.dealloc_raw(second, layout);
            alloc.dealloc_raw(ordinary, layout);
        }
    }

    #[cfg(feature = "stats")]
    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_side_cache_uses_hugepage_mmap_backing() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let slot = segregated_type_cache_slot(metadata, layout);
        let before = system_alloc::hugepage_mmap_stats_snapshot();

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert!(!third.is_null());
        assert_ne!(first, second);
        assert_ne!(first, third);
        assert_ne!(second, third);
        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "the first hugepage metadata free should stay inline"
            );
            alloc.dealloc_with_metadata(second, layout, metadata);
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "the second hugepage metadata free should stay in the bounded inline cache"
            );
            alloc.dealloc_with_metadata(third, layout, metadata);
            assert_eq!(
                SEGREGATED_TYPE_CACHE[slot].count, 0,
                "hugepage metadata must not be stored in the ordinary static side cache"
            );
            assert_eq!(
                hugepage_segregated_bucket_count_for_test(layout, metadata),
                Some(3),
                "the third cached hugepage metadata object should materialize the mmap_huge-backed side cache"
            );
            let snapshot = hugepage_metadata_side_cache_snapshot()
                .expect("hugepage side cache should be live after bucket materialization");
            assert_eq!(
                snapshot.mapping_size,
                snapshot_static_copy(core::ptr::addr_of!(
                    HUGEPAGE_SEGREGATED_TYPE_CACHE_MAPPING_SIZE
                ))
            );
            assert_hugepage_metadata_mapping_size_matches_backing(snapshot);
            let backing = snapshot.backing;
            assert!(
                backing.is_hugepage() || backing.is_fallback(),
                "side-cache backing must distinguish hugepage success from ordinary fallback: {:?}",
                backing
            );
        }

        let after = system_alloc::hugepage_mmap_stats_snapshot();
        assert!(
            after.attempts > before.attempts,
            "hugepage metadata side-cache allocation must attempt mmap_huge"
        );

        let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let reused_three = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_same_pointer_set(
            [first, second, third],
            [reused_one, reused_two, reused_three],
        );
        unsafe {
            alloc.dealloc_raw(reused_one, layout);
            alloc.dealloc_raw(reused_two, layout);
            alloc.dealloc_raw(reused_three, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, 3);
        assert_eq!(snap.typed_cache_hits, 3);
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_side_cache_releases_mapping_after_last_reuse() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6101)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert!(!third.is_null());
        assert_ne!(first, second);
        assert_ne!(first, third);
        assert_ne!(second, third);
        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
        }
        assert!(
            hugepage_metadata_side_cache_snapshot().is_none(),
            "the first cached hugepage metadata object should stay inline"
        );
        unsafe {
            alloc.dealloc_with_metadata(second, layout, metadata);
        }
        assert!(
            hugepage_metadata_side_cache_snapshot().is_none(),
            "the second cached hugepage metadata object should stay in the bounded inline cache"
        );
        unsafe {
            alloc.dealloc_with_metadata(third, layout, metadata);
        }
        assert!(
            hugepage_metadata_side_cache_snapshot().is_some(),
            "the third cached hugepage metadata object should allocate the side-cache mapping"
        );

        let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(
            reused_one == first || reused_one == second || reused_one == third,
            "first reuse should come from one of the cached objects"
        );
        assert!(
            hugepage_metadata_side_cache_snapshot().is_some(),
            "the mmap-backed hugepage side cache should remain while two bucket entries are cached"
        );
        let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(reused_two == first || reused_two == second || reused_two == third);
        assert_ne!(reused_one, reused_two);
        assert!(
            hugepage_metadata_side_cache_snapshot().is_some(),
            "the mmap-backed hugepage side cache should remain while one bucket entry is cached"
        );
        let reused_three = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_same_pointer_set(
            [first, second, third],
            [reused_one, reused_two, reused_three],
        );
        assert!(
            hugepage_metadata_side_cache_snapshot().is_none(),
            "the mmap-backed hugepage side cache should be returned after its last cached object is reused"
        );

        unsafe {
            alloc.dealloc_raw(reused_one, layout);
            alloc.dealloc_raw(reused_two, layout);
            alloc.dealloc_raw(reused_three, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_side_cache_is_thread_local() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            unsafe {
                clear_type_cache_for_test();
                drop_hugepage_segregated_type_cache_for_test();
                clear_delayed_free_for_test();
            }
            semantic_auto_metadata_disable();

            let alloc = RustAllocator::new();
            let layout =
                Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0x5E6D_7101)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

            let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!first.is_null());
            assert!(!second.is_null());
            assert!(!third.is_null());
            assert_ne!(first, second);
            assert_ne!(first, third);
            assert_ne!(second, third);

            unsafe {
                alloc.dealloc_with_metadata(first, layout, metadata);
                assert!(
                    hugepage_metadata_side_cache_snapshot().is_none(),
                    "worker's first hugepage metadata free should remain inline"
                );
                alloc.dealloc_with_metadata(second, layout, metadata);
                assert!(
                    hugepage_metadata_side_cache_snapshot().is_none(),
                    "worker's second hugepage metadata free should remain inline"
                );
                alloc.dealloc_with_metadata(third, layout, metadata);
            }
            let worker_snapshot = hugepage_metadata_side_cache_snapshot()
                .expect("worker should materialize its own hugepage side-cache");
            assert_hugepage_metadata_mapping_size_matches_backing(worker_snapshot);
            ready_tx
                .send((
                    worker_snapshot.address,
                    worker_snapshot.mapping_size,
                    worker_snapshot.backing,
                ))
                .expect("main test thread should wait for worker snapshot");
            release_rx
                .recv_timeout(std::time::Duration::from_secs(10))
                .expect("main test thread should release worker cleanup");

            let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            let reused_three = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert_same_pointer_set(
                [first, second, third],
                [reused_one, reused_two, reused_three],
            );
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "worker side-cache mapping should be released after its last cached entry"
            );

            unsafe {
                alloc.dealloc_raw(reused_one, layout);
                alloc.dealloc_raw(reused_two, layout);
                alloc.dealloc_raw(reused_three, layout);
                drop_hugepage_segregated_type_cache_for_test();
            }
        });

        let (worker_address, worker_mapping_size, worker_backing) = ready_rx
            .recv_timeout(std::time::Duration::from_secs(10))
            .expect("worker should materialize a hugepage side-cache");
        assert_ne!(worker_address, 0);
        assert_hugepage_metadata_mapping_size_matches_backing(HugepageMetadataSideCacheSnapshot {
            allocated: true,
            address: worker_address,
            mapping_size: worker_mapping_size,
            backing: worker_backing,
            occupied_buckets: 0,
            occupied_entries: 0,
            retained_bytes: 0,
            corrupt_buckets: 0,
        });
        assert!(
            hugepage_metadata_side_cache_snapshot().is_none(),
            "main thread must not observe another thread's hugepage side-cache TLS state"
        );

        release_tx
            .send(())
            .expect("worker should still be waiting for cleanup release");
        worker
            .join()
            .expect("worker hugepage side-cache flow should succeed");
        assert!(
            hugepage_metadata_side_cache_snapshot().is_none(),
            "main thread should remain side-cache-free after worker cleanup"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_side_cache_snapshot_reports_retained_bytes() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6201)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let retained = type_cache_retained_bytes_for_layout(layout);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert!(!third.is_null());
        assert_ne!(first, second);
        assert_ne!(first, third);
        assert_ne!(second, third);

        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "the first hugepage metadata free should stay in the inline slot"
            );
            alloc.dealloc_with_metadata(second, layout, metadata);
            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "the second hugepage metadata free should stay in the second inline slot"
            );
            alloc.dealloc_with_metadata(third, layout, metadata);
        }

        let snapshot = hugepage_metadata_side_cache_snapshot()
            .expect("the third cached object should materialize the side-cache mapping");
        assert_eq!(snapshot.occupied_buckets, 1);
        assert_eq!(snapshot.occupied_entries, 3);
        assert_eq!(snapshot.corrupt_buckets, 0);
        assert_eq!(
            snapshot.retained_bytes,
            retained * 3,
            "snapshot must expose allocator-rounded bytes retained in the hugepage side-cache"
        );
        assert!(snapshot.retained_bytes <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES);

        let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let reused_three = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_same_pointer_set(
            [first, second, third],
            [reused_one, reused_two, reused_three],
        );
        assert!(hugepage_metadata_side_cache_snapshot().is_none());

        unsafe {
            alloc.dealloc_raw(reused_one, layout);
            alloc.dealloc_raw(reused_two, layout);
            alloc.dealloc_raw(reused_three, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_side_cache_survives_until_last_cached_entry() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6102)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert!(!third.is_null());
        assert_ne!(first, second);
        assert_ne!(first, third);
        assert_ne!(second, third);
        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            alloc.dealloc_with_metadata(second, layout, metadata);
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
            alloc.dealloc_with_metadata(third, layout, metadata);
        }
        assert_eq!(
            unsafe { hugepage_segregated_bucket_count_for_test(layout, metadata) },
            Some(3),
            "all three freed objects should be cached in the hugepage side-cache bucket"
        );
        assert_eq!(
            hugepage_metadata_side_cache_snapshot()
                .expect("materialized hugepage side-cache should be live")
                .occupied_entries,
            3
        );

        let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(
            reused_one == first || reused_one == second || reused_one == third,
            "first reuse should come from one of the cached objects"
        );
        assert!(
            hugepage_metadata_side_cache_snapshot().is_some(),
            "the mapping must stay while two hugepage side-cache entries remain"
        );
        assert_eq!(
            hugepage_metadata_side_cache_snapshot()
                .unwrap()
                .occupied_entries,
            2
        );

        let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(reused_two == first || reused_two == second || reused_two == third);
        assert_ne!(reused_one, reused_two);
        assert_eq!(
            hugepage_metadata_side_cache_snapshot()
                .unwrap()
                .occupied_entries,
            1
        );
        let reused_three = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_same_pointer_set(
            [first, second, third],
            [reused_one, reused_two, reused_three],
        );
        assert!(
            hugepage_metadata_side_cache_snapshot().is_none(),
            "the mapping should be unmapped after the final hugepage side-cache entry is reused"
        );

        unsafe {
            alloc.dealloc_raw(reused_one, layout);
            alloc.dealloc_raw(reused_two, layout);
            alloc.dealloc_raw(reused_three, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_occupied_bucket_counter_tracks_insert_and_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6103)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

        let first = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let second = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        let third = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!first.is_null());
        assert!(!second.is_null());
        assert!(!third.is_null());
        assert_ne!(first, second);
        assert_ne!(first, third);
        assert_ne!(second, third);

        unsafe {
            alloc.dealloc_with_metadata(first, layout, metadata);
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                0,
                "first hugepage metadata free should stay inline and avoid a bucket"
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
            alloc.dealloc_with_metadata(second, layout, metadata);
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                0,
                "second hugepage metadata free should use the second inline slot"
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
            alloc.dealloc_with_metadata(third, layout, metadata);
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                1,
                "materializing two inline entries plus the third entry should set one occupied bucket"
            );
        }

        let reused_one = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(reused_one == first || reused_one == second || reused_one == third);
        unsafe {
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                1,
                "mapping should stay live while the bucket still has a cached entry"
            );
        }

        let reused_two = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(reused_two == first || reused_two == second || reused_two == third);
        assert_ne!(reused_one, reused_two);
        unsafe {
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                1,
                "the bucket remains occupied until the third and final pop"
            );
        }
        let reused_three = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_same_pointer_set(
            [first, second, third],
            [reused_one, reused_two, reused_three],
        );
        unsafe {
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                0,
                "final pop should update the O(1) empty-cache predicate before unmapping"
            );
            assert!(hugepage_metadata_side_cache_snapshot().is_none());
            alloc.dealloc_raw(reused_one, layout);
            alloc.dealloc_raw(reused_two, layout);
            alloc.dealloc_raw(reused_three, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_release_rechecks_stale_nonzero_empty_counter() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();

            assert!(
                ensure_hugepage_segregated_type_cache().is_some(),
                "test needs a live hugepage/fallback side-cache mapping"
            );
            HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 1;

            release_empty_hugepage_segregated_type_cache();

            assert!(
                hugepage_metadata_side_cache_snapshot().is_none(),
                "an empty hugepage side-cache should not stay mapped because of a stale occupied-bucket counter"
            );
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                0,
                "release should repair the stale occupied-bucket counter"
            );
            drop_hugepage_segregated_type_cache_for_test();
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_release_rechecks_stale_zero_nonempty_counter() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6104)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let ptr = unsafe { alloc.alloc_raw(layout) };
        assert!(!ptr.is_null());

        unsafe {
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let bucket_idx = segregated_type_cache_slot_for_key(cache_key, layout);
            let cache = ensure_hugepage_segregated_type_cache()
                .expect("test needs a live hugepage/fallback side-cache mapping");
            assert!(push_segregated_type_cache_bucket(
                &mut cache[bucket_idx],
                SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key,
                    ptr,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(ptr, layout, metadata),
                    metadata,
                },
            )
            .is_none());
            assert_eq!(hugepage_segregated_occupied_bucket_count_for_test(), 1);

            HUGEPAGE_SEGREGATED_TYPE_CACHE_OCCUPIED_BUCKETS = 0;
            release_empty_hugepage_segregated_type_cache();

            assert!(
                hugepage_metadata_side_cache_snapshot().is_some(),
                "a non-empty hugepage side-cache must not be unmapped because of a stale zero counter"
            );
            assert_eq!(
                hugepage_segregated_occupied_bucket_count_for_test(),
                1,
                "release should repair the stale zero counter from the real bucket state"
            );

            let recovered = pop_segregated_type_cache(layout, metadata)
                .expect("the cached hugepage metadata object should remain recoverable");
            assert_eq!(recovered, ptr);
            alloc.dealloc_raw(recovered, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
    }

    #[cfg(feature = "stats")]
    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_pop_recovers_ordinary_fallback_cache_entries() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let ptr = unsafe { alloc.alloc_raw(layout) };
        assert!(!ptr.is_null());

        unsafe {
            let cache_key = type_cache_identity_key(metadata);
            let ordinary_idx = segregated_type_cache_slot_for_key(cache_key, layout);
            assert!(SEGREGATED_TYPE_CACHE[ordinary_idx]
                .push(SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key: segregated_type_cache_policy_key(metadata),
                    ptr,
                    size: layout.size(),
                    align: layout.align(),
                    auth: metadata_record_auth(ptr, layout, metadata),
                    metadata,
                })
                .is_none());

            assert!(
                ensure_hugepage_segregated_type_cache().is_some(),
                "test needs a live hugepage/fallback side cache mapping"
            );
            assert_eq!(
                hugepage_segregated_bucket_count_for_test(layout, metadata),
                Some(0),
                "the primary hugepage cache starts empty while the older fallback entry lives in the ordinary cache"
            );

            let recovered = pop_segregated_type_cache(layout, metadata)
                .expect("hugepage pop should recover ordinary fallback entry");
            assert_eq!(recovered, ptr);
            assert_eq!(
                SEGREGATED_TYPE_CACHE[ordinary_idx].count, 0,
                "recovering the fallback entry should remove it from the ordinary side cache"
            );
            alloc.dealloc_raw(recovered, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_hits, 1);
        semantic_stats_recording_disable();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn hugepage_metadata_hot_bucket_domain_ignores_stale_ordinary_hint() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            drop_hugepage_segregated_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5E6D_6004)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);
        let ptr = unsafe { alloc.alloc_raw(layout) };
        assert!(!ptr.is_null());

        unsafe {
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let start = segregated_type_cache_slot_for_key(cache_key, layout);
            let stale_ordinary_idx =
                (start + SEGREGATED_TYPE_CACHE_PROBE_LIMIT + 1) & (TYPE_CACHE_SLOTS - 1);

            remember_segregated_type_cache_hot_bucket(
                layout,
                cache_key,
                policy_key,
                SEGREGATED_TYPE_CACHE_DOMAIN_ORDINARY,
                stale_ordinary_idx,
            );
            assert!(
                ensure_hugepage_segregated_type_cache().is_some(),
                "test needs a live hugepage/fallback side-cache mapping"
            );

            assert!(push_segregated_type_cache(ptr, layout, metadata)
                .expect("hugepage metadata side-cache insert")
                .is_none());
            assert_eq!(
                segregated_type_cache_hot_bucket_snapshot_for_test().cache_domain,
                SEGREGATED_TYPE_CACHE_DOMAIN_HUGEPAGE,
                "hugepage side-cache insert should overwrite the stale ordinary-cache hot hint"
            );

            {
                let huge_cache = &*HUGEPAGE_SEGREGATED_TYPE_CACHE;
                assert_eq!(
                    huge_cache[stale_ordinary_idx].count, 0,
                    "a stale ordinary-cache hot bucket outside the probe window must not receive hugepage metadata entries"
                );

                let mut matching_entries = 0;
                let mut offset = 0;
                while offset < SEGREGATED_TYPE_CACHE_PROBE_LIMIT {
                    let idx = (start + offset) & (TYPE_CACHE_SLOTS - 1);
                    matching_entries += huge_cache[idx]
                        .entries
                        .iter()
                        .take(huge_cache[idx].count)
                        .filter(|entry| entry.ptr == ptr)
                        .count();
                    offset += 1;
                }
                assert_eq!(
                    matching_entries, 1,
                    "hugepage metadata entry must remain discoverable inside its deterministic probe window"
                );
            }

            SEGREGATED_TYPE_CACHE_HOT_BUCKET = SegregatedTypeCacheHotBucket::empty();
            let recovered = pop_segregated_type_cache(layout, metadata)
                .expect("entry should be recoverable even after dropping the hugepage hot hint");
            assert_eq!(recovered, ptr);
            alloc.dealloc_raw(recovered, layout);
            drop_hugepage_segregated_type_cache_for_test();
        }
    }

    #[cfg(feature = "stats")]
    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn guard_page_allocations_use_page_mapping_and_bypass_type_cache() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(crate::PAGE_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x6A9D_0001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_GUARD_PAGES);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert_eq!((ptr as usize) & (crate::PAGE_SIZE - 1), 0);
        unsafe {
            ptr.write(0xA5);
            ptr.add(layout.size() - 1).write(0x5A);
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.typed_cache_hits, 0);
        assert_eq!(snap.typed_cache_inserts, 0);
        assert_eq!(snap.typed_cache_bypasses, 0);
        assert!(snap.policy_flags_seen & FLAG_GUARD_PAGES != 0);
        semantic_stats_recording_disable();
    }
    #[cfg(feature = "stats")]
    #[test]
    fn memory_tagging_records_and_clears_matching_allocation() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            assert!(find_memory_tag_slot(ptr).is_some());
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert!(snap.policy_flags_seen & FLAG_MEMORY_TAGGING != 0);
        semantic_stats_recording_disable();
    }

    #[test]
    fn memory_tagging_fast_hot_slot_tracks_recent_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0F45)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let expected_idx = memory_tag_fast_index(ptr);

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("record memory tag in TLS fast table");
            let hot_slot = memory_tag_hot_slot_snapshot_for_test();
            assert_eq!(hot_slot.ptr, ptr as usize);
            assert_eq!(hot_slot.slot_idx, expected_idx);
            assert!(find_memory_tag_slot(ptr).is_some());
            assert_eq!(memory_tag_hot_slot_snapshot_for_test().ptr, ptr as usize);
            clear_memory_tagged_allocation(ptr, layout, metadata);
            assert_eq!(memory_tag_hot_slot_snapshot_for_test().ptr, 0);
            assert!(find_memory_tag_slot(ptr).is_none());
        }
    }

    #[test]
    fn memory_tagging_cross_thread_recovery_uses_global_tag_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_C705)
            .with_module(0xC0DE_7A6D)
            .with_callsite(0xA110_7A6D)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata_hints(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed),
            1,
            "cross-thread tagged allocations must publish the tag record outside allocator TLS"
        );
        unsafe {
            assert!(
                find_memory_tag_slot(ptr).is_none(),
                "the same-thread tag table should stay cold when a cross-thread recovery hint is present"
            );
        }
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), Some(metadata));
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            1,
            "the same cross-thread hint should also publish allocation metadata globally"
        );

        let ptr_addr = ptr as usize;
        let worker = thread::spawn(move || {
            let alloc = RustAllocator::new();
            let ptr = ptr_addr as *mut u8;
            unsafe {
                alloc.dealloc(ptr, layout);
                let cached = pop_semantic_type_cache(layout, metadata)
                    .expect("foreign thread deallocation should still reach typed cache");
                alloc.dealloc_raw(cached, layout);
            }
        });
        worker.join().expect("cross-thread tagged deallocation");

        assert_eq!(GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
    }

    #[test]
    fn memory_tagging_global_hot_slot_tracks_recent_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_610B)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);
        let expected_idx = global_memory_tag_fast_index(ptr);

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("record global memory tag in fast table");
        }
        {
            let mut table = global_memory_tag_table_for_ptr(ptr).lock();
            assert_eq!(table.hot.ptr, ptr as usize);
            assert_eq!(table.hot.slot_idx, expected_idx);
            assert!(find_global_memory_tag_slot(&mut *table, ptr).is_some());
            assert_eq!(table.hot.ptr, ptr as usize);
        }

        assert!(clear_global_memory_tagged_allocation(
            ptr, layout, metadata, true
        ));
        {
            let table = global_memory_tag_table_for_ptr(ptr).lock();
            assert_eq!(table.hot.ptr, 0);
        }
        unsafe {
            clear_memory_tags_for_test();
        }
    }

    #[test]
    fn memory_tagging_global_clear_saturates_stale_zero_count() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_61FF)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("record global memory tag in fast table");
        }
        GLOBAL_MEMORY_TAG_RECORD_COUNT.store(0, Ordering::Relaxed);

        assert!(clear_global_memory_tagged_allocation(
            ptr, layout, metadata, true
        ));
        assert_eq!(
            GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "stale zero count must not underflow to usize::MAX"
        );
        {
            let mut table = global_memory_tag_table_for_ptr(ptr).lock();
            assert!(
                find_global_memory_tag_slot(&mut *table, ptr).is_none(),
                "clearing with a stale zero count should still remove the real tag slot"
            );
        }

        unsafe {
            clear_memory_tags_for_test();
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn memory_tagging_global_shards_accept_parallel_cross_thread_records() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(2 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_5A11)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);
        let mut shard_ptrs = [0usize; GLOBAL_MEMORY_TAG_SHARD_COUNT];
        let all_shards = (1usize << GLOBAL_MEMORY_TAG_SHARD_COUNT) - 1;
        let mut seen = 0usize;
        let mut candidate = 0x5000_0000usize;
        let mut attempts = 0usize;
        while seen != all_shards && attempts < 8192 {
            let ptr = candidate as *mut u8;
            let shard = global_memory_tag_shard(ptr);
            let bit = 1usize << shard;
            if seen & bit == 0 {
                shard_ptrs[shard] = candidate;
                seen |= bit;
            }
            candidate = candidate
                .checked_add(align_of::<usize>())
                .expect("bounded shard-search address space");
            attempts += 1;
        }
        assert_eq!(seen, all_shards, "test must find one address per shard");

        let mut workers = Vec::new();
        for addr in shard_ptrs.iter().copied() {
            workers.push(thread::spawn(move || {
                record_global_memory_tagged_allocation(addr as *mut u8, layout, metadata)
                    .expect("parallel global memory-tag record should fit in its shard");
            }));
        }
        for worker in workers {
            worker
                .join()
                .expect("parallel global memory-tag shard worker");
        }

        assert_eq!(
            GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed),
            GLOBAL_MEMORY_TAG_SHARD_COUNT,
            "each shard should hold one concurrently recorded cross-thread tag"
        );

        for addr in shard_ptrs.iter().copied() {
            assert!(clear_global_memory_tagged_allocation(
                addr as *mut u8,
                layout,
                metadata,
                true
            ));
        }
        assert_eq!(GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed), 0);

        unsafe {
            clear_memory_tags_for_test();
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn memory_tagging_cross_thread_global_spills_to_overflow_after_bounded_fast_probe() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut target_storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let layout = Layout::from_size_align(target_storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_C706)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);
        let ptr = target_storage.as_mut_ptr();
        let start = global_memory_tag_fast_index(ptr);

        {
            let mut table = global_memory_tag_table_for_ptr(ptr).lock();
            for offset in 0..GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT {
                let idx = (start + offset) & (GLOBAL_MEMORY_TAG_SHARD_SLOTS - 1);
                let dummy_ptr =
                    (0x2000usize + (idx + 1) * core::mem::align_of::<usize>()) as *mut u8;
                table.fast[idx] = TaggedAllocation {
                    ptr: dummy_ptr,
                    size: layout.size(),
                    align: layout.align(),
                    tag: 1,
                    metadata,
                };
            }
        }
        GLOBAL_MEMORY_TAG_RECORD_COUNT.store(GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT, Ordering::Relaxed);

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("global memory tag should spill to overflow");
        }

        {
            let mut table = global_memory_tag_table_for_ptr(ptr).lock();
            assert!(
                !table.overflow.is_null(),
                "cross-thread global tags should have the same overflow capacity as the TLS tag table"
            );
            assert!((0..GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT).all(|offset| {
                let idx = (start + offset) & (GLOBAL_MEMORY_TAG_SHARD_SLOTS - 1);
                table.fast[idx].ptr != ptr
            }));
            assert!(find_global_memory_tag_slot(&mut *table, ptr).is_some());
        }

        unsafe {
            clear_memory_tagged_allocation(ptr, layout, metadata);
            assert!(
                GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed)
                    >= GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT
            );
            {
                let table = global_memory_tag_table_for_ptr(ptr).lock();
                assert!(
                    table.overflow.is_null(),
                    "clearing the last global overflow tag should unmap the empty overflow page"
                );
            }
            clear_memory_tags_for_test();
        }
        assert_eq!(GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn memory_tag_fast_tiers_have_bounded_footprint() {
        let tls_fast_table_bytes = size_of::<[TaggedAllocation; MEMORY_TAG_FAST_SLOTS]>();
        let global_fast_table_bytes = GLOBAL_MEMORY_TAG_SHARD_COUNT
            * size_of::<[TaggedAllocation; GLOBAL_MEMORY_TAG_SHARD_SLOTS]>();

        assert!(
            MEMORY_TAG_FAST_SLOTS.is_power_of_two(),
            "fast memory-tag hashing relies on a power-of-two table"
        );
        assert!(
            GLOBAL_MEMORY_TAG_SHARD_COUNT.is_power_of_two()
                && GLOBAL_MEMORY_TAG_SHARD_SLOTS.is_power_of_two(),
            "global memory-tag sharding relies on power-of-two shard geometry"
        );
        assert_eq!(
            GLOBAL_MEMORY_TAG_SHARD_COUNT * GLOBAL_MEMORY_TAG_SHARD_SLOTS,
            MEMORY_TAG_FAST_SLOTS,
            "sharding must not grow the global fast-tier record budget"
        );
        assert!(
            MEMORY_TAG_FAST_PROBE_LIMIT <= MEMORY_TAG_FAST_SLOTS,
            "TLS bounded probing must fit inside the fast table"
        );
        assert!(
            GLOBAL_MEMORY_TAG_FAST_PROBE_LIMIT <= GLOBAL_MEMORY_TAG_SHARD_SLOTS,
            "global bounded probing must fit inside one shard"
        );
        assert_eq!(
            global_fast_table_bytes, tls_fast_table_bytes,
            "8 global shards should keep the same aggregate fast-tier footprint"
        );
        assert!(
            tls_fast_table_bytes <= 16 * 1024,
            "memory-tag fast tier should not reserve more than 16KiB per TLS table"
        );
        assert!(
            global_fast_table_bytes <= 16 * 1024,
            "global shards should not reserve more than the old single-table footprint"
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn memory_tag_overflow_page_has_bounded_footprint() {
        assert!(
            MEMORY_TAG_PAGE_SLOTS <= 128,
            "overflow pages should stay small enough for cold spill metadata"
        );
        assert!(
            size_of::<MemoryTagPage>() <= 9 * 1024,
            "memory-tag overflow page metadata should be about one small page of records"
        );
    }

    #[test]
    fn memory_tagging_record_tag_includes_process_cookie() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        reset_metadata_auth_cookie_for_test();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_C001)
            .with_module(0xC0DE_7A6D)
            .with_callsite(0xA110_7A6D)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_lifetime_hint(0x61)
            .with_placement_hint(0x62);
        let keyed = derive_memory_tag(ptr, layout, metadata);
        let legacy = derive_memory_tag_without_cookie_for_test(ptr, layout, metadata);
        assert_ne!(keyed, 0);
        assert_ne!(
            keyed, legacy,
            "software memory tag must include the process cookie, not only public record fields"
        );
        assert_eq!(
            keyed,
            derive_memory_tag(ptr, layout, metadata),
            "the process cookie must be stable for outstanding memory-tag records"
        );

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("record memory tag with process-cookie auth");
            let slot = find_memory_tag_slot(ptr).expect("recorded memory tag");
            assert_eq!((*slot).tag, keyed);
            (*slot).tag = legacy;
            assert!(
                std::panic::catch_unwind(|| verify_memory_tag_record(*slot)).is_err(),
                "a legacy public hash must not spoof a memory-tag record"
            );
            (*slot).tag = keyed;
            clear_memory_tagged_allocation(ptr, layout, metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
        }
    }

    #[test]
    fn memory_tagging_corrupt_record_validation_preserves_record_for_recovery() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_C002)
            .with_module(0xC0DE_C002)
            .with_callsite(0xA110_C002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata).expect("record memory tag");
            let slot = find_memory_tag_slot(ptr).expect("recorded memory tag");
            let valid_tag = (*slot).tag;
            (*slot).tag = valid_tag ^ 1;
            assert_eq!(
                try_verify_memory_tag_record(*slot),
                Err(MemoryTagValidationError::RecordCorrupt),
                "low-level validation should classify corrupt records before fail-stop"
            );

            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                clear_memory_tagged_allocation(ptr, layout, metadata);
            }));
            assert!(
                result.is_err(),
                "corrupt memory-tag records must still fail-stop"
            );
            assert!(
                find_memory_tag_slot(ptr).is_some(),
                "corrupt-record validation must not clear the tag before panicking"
            );

            let slot = find_memory_tag_slot(ptr).expect("record remains after failed validation");
            (*slot).tag = valid_tag;
            clear_memory_tagged_allocation(ptr, layout, metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
        }
    }

    #[test]
    fn memory_tagging_places_record_at_hashed_fast_slot() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0A11)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            let expected_idx = memory_tag_fast_index(ptr);
            assert_eq!(MEMORY_TAGS[expected_idx].ptr, ptr);
            assert!(find_memory_tag_slot(ptr).is_some());
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert!(MEMORY_TAGS[expected_idx].is_empty());
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn memory_tagging_rejects_duplicate_record_during_reserve() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0A12)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let ptr = storage.as_mut_ptr();
        unsafe {
            assert_eq!(
                record_memory_tagged_allocation(ptr, layout, metadata),
                Ok(())
            );
            assert_eq!(
                record_memory_tagged_allocation(ptr, layout, metadata),
                Err(MemoryTagReservationError::DuplicateAllocationRecord)
            );
            clear_memory_tagged_allocation(ptr, layout, metadata);
        }
    }

    #[test]
    fn memory_tagging_finish_fail_closes_duplicate_record_without_panic() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0A14)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let ptr = unsafe { alloc.alloc_raw(layout) };
        assert!(!ptr.is_null(), "test needs a real raw allocation");

        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("pre-existing memory tag record");
            let finished =
                alloc.finish_semantic_allocation_with_recovery(ptr, layout, metadata, true);
            assert!(
                finished.is_null(),
                "a tagged allocation without record capacity must fail closed instead of returning an untracked pointer"
            );
            assert_eq!(
                lookup_auto_allocation_metadata(ptr, layout),
                None,
                "failed allocation finishing must roll back compiler recovery metadata"
            );
            assert!(
                find_memory_tag_slot(ptr).is_some(),
                "the stale tag record that caused the duplicate failure remains isolated for explicit cleanup"
            );
            clear_memory_tagged_allocation(ptr, layout, metadata);
        }
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn memory_tagging_allocation_fail_closes_when_tag_table_full() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0A15)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let probe = unsafe { alloc.alloc_raw(layout) };
        assert!(
            !probe.is_null(),
            "fixed-heap allocator must be live for this test"
        );
        unsafe {
            alloc.dealloc_raw(probe, layout);
            for idx in 0..MEMORY_TAG_FAST_SLOTS {
                MEMORY_TAGS[idx] = TaggedAllocation {
                    ptr: (0x1000_0000usize + (idx + 1) * align_of::<usize>()) as *mut u8,
                    size: layout.size(),
                    align: layout.align(),
                    tag: 1,
                    metadata,
                };
            }
            MEMORY_TAG_RECORD_COUNT = MEMORY_TAG_FAST_SLOTS;
        }

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(
            ptr.is_null(),
            "fixed-heap memory tagging must fail closed when the bounded tag side table is full"
        );
        unsafe {
            clear_memory_tags_for_test();
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn memory_tagging_spills_to_overflow_after_bounded_fast_probe() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut target_storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let layout = Layout::from_size_align(target_storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0A13)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let ptr = target_storage.as_mut_ptr();
        let start = memory_tag_fast_index(ptr);

        unsafe {
            for offset in 0..MEMORY_TAG_FAST_PROBE_LIMIT {
                let idx = (start + offset) & (MEMORY_TAG_FAST_SLOTS - 1);
                let dummy_ptr =
                    (0x1000usize + (idx + 1) * core::mem::align_of::<usize>()) as *mut u8;
                MEMORY_TAGS[idx] = TaggedAllocation {
                    ptr: dummy_ptr,
                    size: layout.size(),
                    align: layout.align(),
                    tag: 1,
                    metadata,
                };
            }

            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("memory tag should spill to overflow");
            assert!(!MEMORY_TAG_OVERFLOW.is_null());
            assert!((0..MEMORY_TAG_FAST_PROBE_LIMIT).all(|offset| {
                let idx = (start + offset) & (MEMORY_TAG_FAST_SLOTS - 1);
                MEMORY_TAGS[idx].ptr != ptr
            }));
            assert!(find_memory_tag_slot(ptr).is_some());

            clear_memory_tagged_allocation(ptr, layout, metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
            assert!(
                MEMORY_TAG_OVERFLOW.is_null(),
                "clearing the last TLS overflow tag should unmap the empty overflow page"
            );
            clear_memory_tags_for_test();
        }
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn memory_tagging_full_fast_tier_spills_to_bounded_overflow() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut target_storage = [0u8; 2 * core::mem::size_of::<usize>()];
        let layout = Layout::from_size_align(target_storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_0A16)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let ptr = target_storage.as_mut_ptr();

        unsafe {
            for idx in 0..MEMORY_TAG_FAST_SLOTS {
                MEMORY_TAGS[idx] = TaggedAllocation {
                    ptr: (0x3000_0000usize + (idx + 1) * align_of::<usize>()) as *mut u8,
                    size: layout.size(),
                    align: layout.align(),
                    tag: 1,
                    metadata,
                };
            }
            MEMORY_TAG_RECORD_COUNT = MEMORY_TAG_FAST_SLOTS;

            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("full fast memory-tag tier should spill to overflow on hosted targets");
            assert!(!MEMORY_TAG_OVERFLOW.is_null());
            assert!(
                (0..MEMORY_TAG_FAST_SLOTS).all(|idx| MEMORY_TAGS[idx].ptr != ptr),
                "the new record should not displace an occupied fast-tier record"
            );
            assert!(
                find_memory_tag_slot(ptr).is_some(),
                "overflow record must remain discoverable for checked deallocation"
            );

            clear_memory_tagged_allocation(ptr, layout, metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
            assert!(
                MEMORY_TAG_OVERFLOW.is_null(),
                "full-fast-tier overflow page should be released after its last live tag is cleared"
            );
            clear_memory_tags_for_test();
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn memory_tagging_allows_unknown_auto_dealloc_metadata() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_0002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let dealloc_metadata = AllocationMetadata::unknown().with_flags(FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, dealloc_metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.fallback_deallocations, 1);
        semantic_stats_recording_disable();
    }

    #[test]
    #[should_panic(expected = "memory tag type mismatch")]
    fn memory_tagging_rejects_wrong_typed_dealloc_metadata() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_0003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(0x7A6D_0004)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, wrong_dealloc_metadata);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn memory_tagging_rejected_dealloc_does_not_count_stats() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_00A3)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(0x7A6D_00A4)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| unsafe {
            alloc.dealloc_with_metadata(ptr, layout, wrong_dealloc_metadata);
        }));
        assert!(
            result.is_err(),
            "wrong memory-tag metadata must fail before deallocation is accepted"
        );

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.fallback_deallocations, 0);

        unsafe {
            clear_memory_tags_for_test();
            alloc.dealloc_raw(ptr, layout);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn memory_tagging_rejected_type_dealloc_preserves_record_for_correct_retry() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_10A3)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(0x7A6D_10A4)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| unsafe {
            alloc.dealloc_with_metadata(ptr, layout, wrong_dealloc_metadata);
        }));
        assert!(result.is_err(), "wrong typed metadata must still fail-stop");

        unsafe {
            assert!(
                find_memory_tag_slot(ptr).is_some(),
                "failed typed deallocation must not clear the tag record"
            );
            alloc.dealloc_with_metadata(ptr, layout, alloc_metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
        }
    }

    #[test]
    fn memory_tagging_rejected_layout_dealloc_preserves_record_for_correct_retry() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let wrong_layout =
            Layout::from_size_align(size_of::<usize>() * 2, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x7A6D_10A5)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| unsafe {
            alloc.dealloc_with_metadata(ptr, wrong_layout, metadata);
        }));
        assert!(
            result.is_err(),
            "layout-mismatched deallocation must still fail-stop"
        );

        unsafe {
            assert!(
                find_memory_tag_slot(ptr).is_some(),
                "failed layout validation must happen before clearing the tag record"
            );
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert!(find_memory_tag_slot(ptr).is_none());
        }
    }

    #[test]
    fn memory_tagging_global_rejected_dealloc_preserves_record_for_correct_retry() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_memory_tags_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_10A6)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(0x7A6D_10A7)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        assert_eq!(GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed), 1);

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| unsafe {
            alloc.dealloc_with_metadata(ptr, layout, wrong_dealloc_metadata);
        }));
        assert!(
            result.is_err(),
            "wrong global memory-tag metadata must still fail-stop"
        );

        {
            let mut table = global_memory_tag_table_for_ptr(ptr).lock();
            assert!(
                find_global_memory_tag_slot(&mut *table, ptr).is_some(),
                "failed global validation must not clear the cross-thread tag record"
            );
        }
        assert_eq!(GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed), 1);

        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, alloc_metadata);
        }
        assert_eq!(GLOBAL_MEMORY_TAG_RECORD_COUNT.load(Ordering::Relaxed), 0);
    }

    #[test]
    #[should_panic(expected = "memory tag metadata mismatch")]
    fn memory_tagging_rejects_same_type_different_module_dealloc_metadata() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_0005)
            .with_module(0xC0DE_A)
            .with_callsite(0xA110_7A6D)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(alloc_metadata.type_id)
            .with_module(0xC0DE_B)
            .with_callsite(alloc_metadata.callsite)
            .with_flags(alloc_metadata.flags);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, wrong_dealloc_metadata);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn memory_tagging_accepts_compatible_drop_callsite_dealloc_metadata() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let alloc_metadata = AllocationMetadata::for_type(0x7A6D_0006)
            .with_module(0xC0DE_A)
            .with_callsite(0xA110_7A6E)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let drop_metadata = AllocationMetadata::for_type(alloc_metadata.type_id)
            .with_module(alloc_metadata.module_id)
            .with_callsite(0xD0D0_7A6E)
            .with_flags(alloc_metadata.flags);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, alloc_metadata) };
        assert!(!ptr.is_null());
        unsafe {
            assert!(find_memory_tag_slot(ptr).is_some());
            alloc.dealloc_with_metadata(ptr, layout, drop_metadata);
            assert!(
                find_memory_tag_slot(ptr).is_none(),
                "compatible compiler Drop-site metadata should clear the recorded memory tag"
            );
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        semantic_stats_recording_disable();
    }

    fn derive_metadata_record_auth_without_cookie_for_test(
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> u64 {
        let hash = fnv1a_mix(FNV1A_OFFSET, b"semantic-metadata-auth-v1");
        let hash = fnv1a_mix(hash, &(ptr as usize).to_le_bytes());
        let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
        let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
        non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true))
    }

    fn derive_auto_allocation_record_auth_without_cookie_for_test(
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> u64 {
        let hash = fnv1a_mix(FNV1A_OFFSET, b"semantic-auto-allocation-record-v1");
        let hash = fnv1a_mix(hash, &(ptr as usize).to_le_bytes());
        let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
        let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
        non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true))
    }

    fn derive_memory_tag_without_cookie_for_test(
        ptr: *mut u8,
        layout: Layout,
        metadata: AllocationMetadata,
    ) -> u64 {
        let hash = fnv1a_mix(FNV1A_OFFSET, b"semantic-memory-tag-v1");
        let hash = fnv1a_mix(hash, &(ptr as usize).to_le_bytes());
        let hash = fnv1a_mix(hash, &layout.size().to_le_bytes());
        let hash = fnv1a_mix(hash, &layout.align().to_le_bytes());
        non_zero_hash(mix_semantic_metadata_fields(hash, metadata, true))
    }

    #[test]
    fn metadata_software_auth_includes_process_cookie() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        reset_metadata_auth_cookie_for_test();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let mut storage = [0u8; 4 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_C001)
            .with_module(0xC0DE_C001)
            .with_callsite(0xA110_C001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION);

        let keyed = derive_metadata_record_auth(ptr, layout, metadata);
        let legacy = derive_metadata_record_auth_without_cookie_for_test(ptr, layout, metadata);
        assert_ne!(keyed, 0);
        assert_ne!(
            keyed, legacy,
            "software metadata auth must include the process cookie, not only public metadata fields"
        );
        assert_eq!(
            keyed,
            derive_metadata_record_auth(ptr, layout, metadata),
            "metadata auth cookie must be stable for cached metadata records"
        );
    }

    #[test]
    fn metadata_software_auth_binds_pointer_layout_and_all_metadata_fields() {
        let _guard = test_guard();
        reset_metadata_auth_cookie_for_test();

        let mut storage = [0u8; 8 * core::mem::size_of::<usize>()];
        let ptr = storage.as_mut_ptr();
        let layout = Layout::from_size_align(storage.len(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_C007)
            .with_module(0xC0DE_C007)
            .with_callsite(0xA110_C007)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x17)
            .with_placement_hint(0x2A);
        let auth = derive_metadata_record_auth(ptr, layout, metadata);

        assert_ne!(
            auth,
            derive_metadata_record_auth(ptr.wrapping_add(1), layout, metadata),
            "software auth must bind the allocation pointer"
        );
        let different_size = Layout::from_size_align(layout.size() / 2, layout.align()).unwrap();
        assert_ne!(
            auth,
            derive_metadata_record_auth(ptr, different_size, metadata),
            "software auth must bind layout size"
        );
        let different_align =
            Layout::from_size_align(layout.size(), layout.align().saturating_mul(2)).unwrap();
        assert_ne!(
            auth,
            derive_metadata_record_auth(ptr, different_align, metadata),
            "software auth must bind layout alignment"
        );

        let metadata_variants = [
            AllocationMetadata::for_type(metadata.type_id ^ 1)
                .with_module(metadata.module_id)
                .with_callsite(metadata.callsite)
                .with_flags(metadata.flags)
                .with_lifetime_hint(metadata.lifetime_hint)
                .with_placement_hint(metadata.placement_hint),
            metadata.with_module(metadata.module_id ^ 1),
            metadata.with_callsite(metadata.callsite ^ 1),
            metadata.with_flags(metadata.flags ^ FLAG_FORCE_INITIALIZE),
            metadata.with_lifetime_hint(metadata.lifetime_hint ^ 1),
            metadata.with_placement_hint(metadata.placement_hint ^ 1),
        ];
        for changed in metadata_variants {
            assert_ne!(
                auth,
                derive_metadata_record_auth(ptr, layout, changed),
                "software auth must bind every semantic metadata field"
            );
        }
        assert_eq!(
            METADATA_RECORD_AUTH_COMPUTATIONS.load(Ordering::Relaxed),
            10,
            "each changed pointer/layout/metadata tuple must recompute auth"
        );

        assert_eq!(auth, derive_metadata_record_auth(ptr, layout, metadata));
        assert_eq!(
            METADATA_RECORD_AUTH_COMPUTATIONS.load(Ordering::Relaxed),
            11,
            "returning to the original tuple after another tuple must recompute auth"
        );
        assert_eq!(auth, derive_metadata_record_auth(ptr, layout, metadata));
        #[cfg(not(unialloc_target_arm64e))]
        assert_eq!(
            METADATA_RECORD_AUTH_COMPUTATIONS.load(Ordering::Relaxed),
            11,
            "an immediately repeated exact tuple should hit the auth memo"
        );
        #[cfg(unialloc_target_arm64e)]
        assert_eq!(
            METADATA_RECORD_AUTH_COMPUTATIONS.load(Ordering::Relaxed),
            12,
            "arm64e deliberately avoids the TLS auth memo and must recompute"
        );
    }

    #[cfg(all(feature = "stats", feature = "pac", target_arch = "aarch64"))]
    #[test]
    fn metadata_pointer_auth_hot_reuse_derives_static_auth_seed_once() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        reset_metadata_auth_cookie_for_test();
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let software_fallback = !crate::pal::arch::pac::context_binding_available();
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_C006)
            .with_module(0xC0DE_C006)
            .with_callsite(0xA110_C006)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_METADATA_PROTECTION);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(
                reused, ptr,
                "PAC hot-path regression must exercise cache reuse"
            );
            alloc.dealloc_with_metadata(reused, layout, metadata);
        }

        let snap = semantic_stats_snapshot();
        if software_fallback {
            assert_eq!(snap.metadata_pac_auth_signs, 0);
            assert_eq!(snap.metadata_pac_auth_verifications, 0);
            assert_eq!(snap.metadata_pac_auth_failures, 0);
            assert_eq!(snap.metadata_pac_software_fallback_signs, 2);
            assert_eq!(snap.metadata_pac_software_fallback_verifications, 1);
            assert_eq!(snap.metadata_pac_software_fallback_failures, 0);
            assert_eq!(
                METADATA_RECORD_AUTH_SEED_DERIVATIONS.load(Ordering::Relaxed),
                1,
                "software PAC hot reuse should derive the stable domain/cookie seed once"
            );
            assert_eq!(
                METADATA_RECORD_AUTH_COMPUTATIONS.load(Ordering::Relaxed),
                1,
                "software PAC hot reuse should compute an unchanged full-tuple auth once"
            );
        } else {
            assert_eq!(snap.metadata_pac_auth_signs, 2);
            assert_eq!(snap.metadata_pac_auth_verifications, 1);
            assert_eq!(snap.metadata_pac_auth_failures, 0);
            assert_eq!(snap.metadata_pac_software_fallback_signs, 0);
            assert_eq!(snap.metadata_pac_software_fallback_verifications, 0);
            assert_eq!(snap.metadata_pac_software_fallback_failures, 0);
            assert_eq!(
                METADATA_RECORD_AUTH_SEED_DERIVATIONS.load(Ordering::Relaxed),
                0,
                "hardware PAC hot reuse must not derive the software fallback seed"
            );
            assert_eq!(
                METADATA_RECORD_AUTH_COMPUTATIONS.load(Ordering::Relaxed),
                0,
                "hardware PAC hot reuse must not compute software fallback auth"
            );
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn metadata_auth_cookie_initializes_once_across_threads() {
        let _guard = test_guard();
        reset_metadata_auth_cookie_for_test();

        let mut workers = Vec::new();
        for _ in 0..8 {
            workers.push(thread::spawn(metadata_auth_cookie));
        }

        let first = workers
            .remove(0)
            .join()
            .expect("metadata cookie worker should not panic");
        assert_ne!(first, 0);
        for worker in workers {
            assert_eq!(
                worker
                    .join()
                    .expect("metadata cookie worker should not panic"),
                first,
                "metadata auth cookie must be stable even when first initialized concurrently"
            );
        }
        assert_eq!(metadata_auth_cookie(), first);
    }

    #[test]
    fn keyed_integrity_hash_is_stable_and_domain_separated() {
        let _guard = test_guard();
        reset_metadata_auth_cookie_for_test();

        let metadata_record = keyed_integrity_hash(b"semantic-metadata-auth-v1");
        let auto_record = keyed_integrity_hash(b"semantic-auto-allocation-record-v1");
        let memory_tag = keyed_integrity_hash(b"semantic-memory-tag-v1");

        assert_ne!(metadata_record, 0);
        assert_ne!(auto_record, 0);
        assert_ne!(memory_tag, 0);
        assert_ne!(
            metadata_record, auto_record,
            "metadata records and compiler recovery records must not share the same keyed domain"
        );
        assert_ne!(
            metadata_record, memory_tag,
            "metadata records and memory-tag records must not share the same keyed domain"
        );
        assert_ne!(
            auto_record, memory_tag,
            "compiler recovery records and memory-tag records must not share the same keyed domain"
        );
        assert_eq!(
            metadata_record,
            keyed_integrity_hash(b"semantic-metadata-auth-v1"),
            "keyed domain hashes must be stable while records are outstanding"
        );
    }

    #[test]
    #[should_panic(expected = "segregated metadata side-table layout corrupt")]
    fn segregated_metadata_eviction_rejects_corrupt_layout_before_raw_free() {
        let _guard = test_guard();
        let alloc = RustAllocator::new();
        let slot = SegregatedTypeCacheEntry {
            cache_key: 0xC004_F411,
            type_id: 0xC004_F411,
            policy_key: segregated_type_cache_policy_key(
                AllocationMetadata::for_type(0xC004_F411)
                    .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED),
            ),
            ptr: NonNull::<u8>::dangling().as_ptr(),
            size: 16,
            align: 3,
            auth: 0,
            metadata: AllocationMetadata::for_type(0xC004_F411)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED),
        };

        unsafe {
            release_evicted_segregated_type_cache_entry(&alloc, slot);
        }
    }

    #[test]
    #[should_panic(expected = "memory tag side-table layout corrupt")]
    fn memory_tag_verification_rejects_corrupt_record_layout() {
        let _guard = test_guard();
        let record = TaggedAllocation {
            ptr: NonNull::<u8>::dangling().as_ptr(),
            size: 16,
            align: 3,
            tag: 0,
            metadata: AllocationMetadata::for_type(0xC006_F411)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING),
        };

        unsafe {
            verify_memory_tag_record(record);
        }
    }

    #[test]
    #[should_panic(expected = "delayed free side-table layout corrupt")]
    fn delayed_free_release_rejects_corrupt_record_layout() {
        let _guard = test_guard();
        let alloc = RustAllocator::new();
        let slot = DelayedFreeSlot {
            ptr: NonNull::<u8>::dangling().as_ptr(),
            size: 16,
            align: 3,
            auth: 0,
            metadata: AllocationMetadata::for_type(0xC003_F411)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE),
        };

        unsafe {
            release_delayed_slot(&alloc, slot);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn metadata_pointer_auth_accepts_valid_segregated_side_entry() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert_eq!(
                ordinary_inline_segregated_type_cache_entry_snapshot_for_test().ptr,
                ptr,
                "ordinary pointer-auth metadata should use the O(1) inline side-cache"
            );
            let slot = cached_segregated_type_cache_entry_for_test(layout, metadata, ptr)
                .expect("cached protected side entry");
            assert_ne!((*slot).auth, 0);
            assert_eq!(
                SEGREGATED_TYPE_CACHE[segregated_type_cache_slot(metadata, layout)].count,
                0,
                "single hot pointer-auth entry should avoid the bucket side-cache"
            );

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            assert!(INLINE_SEGREGATED_TYPE_CACHE_ENTRY.is_empty());
            alloc.dealloc_raw(reused, layout);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_cache_inserts, 1);
        assert_eq!(snap.typed_cache_hits, 1);
        assert!(snap.policy_flags_seen & FLAG_POINTER_AUTH != 0);
        semantic_stats_recording_disable();
    }

    #[cfg(all(feature = "pac", target_arch = "aarch64"))]
    #[test]
    fn metadata_pointer_auth_uses_aarch64_pac_or_safe_hash_fallback() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();
        let pac_context_binding_active = crate::pal::arch::pac::context_binding_available();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let slot = cached_segregated_type_cache_entry_for_test(layout, metadata, ptr)
                .expect("cached protected side entry");
            assert_ne!((*slot).auth, 0);
            if pac_context_binding_active {
                assert_eq!(
                    crate::pal::arch::pac::xpaci((*slot).auth as usize),
                    ptr as usize
                );
                assert_ne!(
                    (*slot).auth,
                    derive_metadata_record_auth(ptr, layout, metadata),
                    "active PAC should store the hardware signature, not the software hash fallback"
                );
            } else {
                assert_eq!(
                    (*slot).auth,
                    derive_metadata_record_auth(ptr, layout, metadata),
                    "PAC without enforced context binding must fail closed to the software hash"
                );
            }

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            alloc.dealloc_raw(reused, layout);
        }

        let snap = semantic_stats_snapshot();
        if pac_context_binding_active {
            assert_eq!(snap.metadata_pac_auth_signs, 1);
            assert_eq!(snap.metadata_pac_auth_verifications, 1);
            assert_eq!(snap.metadata_pac_software_fallback_signs, 0);
            assert_eq!(snap.metadata_pac_software_fallback_verifications, 0);
        } else {
            assert_eq!(
                snap.metadata_pac_auth_signs, 0,
                "software fallback must not be reported as PAC signing"
            );
            assert_eq!(
                snap.metadata_pac_auth_verifications, 0,
                "software fallback must not be reported as PAC verification"
            );
            assert_eq!(
                snap.metadata_pac_software_fallback_signs, 1,
                "PAC requests on no-op targets must be accounted as software fallback signing"
            );
            assert_eq!(
                snap.metadata_pac_software_fallback_verifications, 1,
                "PAC requests on no-op targets must be accounted as software fallback verification"
            );
        }
        assert_eq!(snap.metadata_pac_auth_failures, 0);
        assert_eq!(snap.metadata_pac_software_fallback_failures, 0);
        semantic_stats_recording_disable();
    }

    #[cfg(all(feature = "pac", target_arch = "aarch64"))]
    #[test]
    fn metadata_protection_without_pointer_auth_uses_software_hash_not_pac() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0005)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let slot = cached_segregated_type_cache_entry_for_test(layout, metadata, ptr)
                .expect("cached metadata-protected side entry");
            assert_eq!(
                (*slot).auth,
                derive_metadata_record_auth(ptr, layout, metadata)
            );

            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            alloc.dealloc_raw(reused, layout);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(
            snap.metadata_pac_auth_signs, 0,
            "metadata-protection-only entries should not be counted as PAC signatures"
        );
        assert_eq!(
            snap.metadata_pac_auth_verifications, 0,
            "metadata-protection-only validation should not be counted as PAC verification"
        );
        assert_eq!(snap.metadata_pac_auth_failures, 0);
        assert_eq!(
            snap.metadata_pac_software_fallback_signs, 0,
            "metadata-protection-only entries should not be counted as PAC fallback signatures"
        );
        assert_eq!(
            snap.metadata_pac_software_fallback_verifications, 0,
            "metadata-protection-only entries should not be counted as PAC fallback verification"
        );
        assert_eq!(snap.metadata_pac_software_fallback_failures, 0);
        semantic_stats_recording_disable();
    }

    #[cfg(all(feature = "pac", target_arch = "aarch64"))]
    #[test]
    fn metadata_pointer_auth_stats_do_not_pollute_disabled_measurements() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0004)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let reused = alloc.alloc_with_metadata(layout, metadata);
            assert_eq!(reused, ptr);
            alloc.dealloc_raw(reused, layout);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.typed_cache_hits, 0);
        assert_eq!(snap.metadata_pac_auth_signs, 0);
        assert_eq!(snap.metadata_pac_auth_verifications, 0);
        assert_eq!(snap.metadata_pac_auth_failures, 0);
        assert_eq!(snap.metadata_pac_software_fallback_signs, 0);
        assert_eq!(snap.metadata_pac_software_fallback_verifications, 0);
        assert_eq!(snap.metadata_pac_software_fallback_failures, 0);
    }

    #[test]
    #[should_panic(expected = "metadata integrity check failed")]
    fn metadata_pointer_auth_rejects_corrupted_segregated_side_entry() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let slot = cached_segregated_type_cache_entry_for_test(layout, metadata, ptr)
                .expect("cached protected side entry");
            (*slot).auth ^= 1;
            let _ = alloc.alloc_with_metadata(layout, metadata);
        }
    }

    #[test]
    #[should_panic(expected = "metadata integrity check failed")]
    fn metadata_pointer_auth_rejects_stale_auth_after_metadata_tamper() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0006)
            .with_module(0xC0DE_A17C)
            .with_callsite(0xA110_A17C)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            {
                let slot = cached_segregated_type_cache_entry_for_test(layout, metadata, ptr)
                    .expect("cached protected side entry");
                let original_auth = (*slot).auth;
                assert_ne!(original_auth, 0);
                (*slot).metadata = (*slot).metadata.with_module(0xC0DE_B17C);
                assert_eq!(
                    (*slot).auth,
                    original_auth,
                    "test must keep the old authenticator while tampering metadata"
                );
            }
            let _ = alloc.alloc_with_metadata(layout, metadata);
        }
    }

    #[test]
    #[should_panic(expected = "metadata integrity check failed")]
    fn metadata_protection_rejects_corrupted_delayed_free_entry() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE | FLAG_METADATA_PROTECTION);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert_eq!(DELAYED_FREE[0].ptr, ptr);
            DELAYED_FREE[0].auth ^= 1;
            release_delayed_slot(&alloc, DELAYED_FREE[0]);
        }
    }

    #[test]
    #[should_panic(expected = "metadata integrity check failed")]
    fn metadata_protection_rejects_stale_auth_after_delayed_free_metadata_tamper() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xA17C_0007)
            .with_module(0xC0DE_D17C)
            .with_callsite(0xA110_D17C)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE | FLAG_METADATA_PROTECTION);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            assert_eq!(DELAYED_FREE[0].ptr, ptr);
            let original_auth = DELAYED_FREE[0].auth;
            assert_ne!(original_auth, 0);
            DELAYED_FREE[0].metadata = DELAYED_FREE[0].metadata.with_callsite(0xD0D0_D17C);
            assert_eq!(
                DELAYED_FREE[0].auth, original_auth,
                "test must keep the old authenticator while tampering delayed-free metadata"
            );
            release_delayed_slot(&alloc, DELAYED_FREE[0]);
        }
    }

    #[test]
    fn stats_compute_basis_point_coverage() {
        let _guard = test_guard();
        let stats = SemanticStats::new();
        stats.record_alloc(AllocationMetadata::unknown(), 16);
        stats.record_alloc(AllocationMetadata::for_type(1), 32);
        stats.record_dealloc(AllocationMetadata::unknown());
        stats.record_dealloc(AllocationMetadata::for_type(1));
        let snap = stats.snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.coverage_basis_points, 5000);
        assert_eq!(snap.total_allocated_bytes, 48);
        assert_eq!(snap.typed_allocated_bytes, 32);
        assert_eq!(snap.fallback_allocated_bytes, 16);
        assert_eq!(snap.total_deallocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 1);
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_stats_checked_snapshot_is_size_negotiated() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let metadata = AllocationMetadata::for_type(0xC002_5AFE)
            .with_module(0x5354_4154)
            .with_callsite(0x5AFE_AB1);
        record_stats_alloc(metadata, 64);
        record_stats_dealloc(metadata);

        assert_eq!(
            __unialloc_semantic_stats_snapshot_abi_version(),
            SEMANTIC_STATS_SNAPSHOT_ABI_VERSION
        );
        assert_eq!(
            __unialloc_semantic_stats_snapshot_size(),
            size_of::<SemanticStatsSnapshot>()
        );

        let mut short = MaybeUninit::<SemanticStatsSnapshot>::uninit();
        assert!(
            !unsafe {
                __unialloc_semantic_stats_snapshot_checked(
                    short.as_mut_ptr(),
                    size_of::<SemanticStatsSnapshot>() - 1,
                )
            },
            "checked snapshot must reject undersized caller buffers instead of writing past them"
        );

        let mut out = MaybeUninit::<SemanticStatsSnapshot>::uninit();
        assert!(unsafe {
            __unialloc_semantic_stats_snapshot_checked(
                out.as_mut_ptr(),
                size_of::<SemanticStatsSnapshot>(),
            )
        });
        let snap = unsafe { out.assume_init() };
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.total_deallocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);

        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_checked_snapshot_requires_exact_record_size() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        semantic_auto_metadata_disable();
        semantic_stats_reset();
        semantic_type_stats_reset();
        semantic_stats_recording_enable();
        semantic_type_stats_recording_enable();

        let metadata = AllocationMetadata::for_type(0x5151_7001)
            .with_module(0x5151_7002)
            .with_callsite(0x5151_7003)
            .with_flags(FLAG_TYPE_ISOLATED);
        record_stats_alloc(metadata, 64);
        record_stats_dealloc(metadata);

        assert_eq!(
            __unialloc_semantic_type_stats_snapshot_abi_version(),
            SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION
        );
        assert_eq!(
            __unialloc_semantic_type_stats_snapshot_record_size(),
            size_of::<SemanticTypeStatsSnapshot>()
        );

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: u64::MAX,
            module_id: u64::MAX,
            callsite: u64::MAX,
            allocations: usize::MAX,
            allocated_bytes: usize::MAX,
            deallocations: usize::MAX,
            cache_hits: usize::MAX,
            cache_inserts: usize::MAX,
            cache_bypasses: usize::MAX,
            observed_alloc_size: usize::MAX,
            observed_alloc_align: usize::MAX,
            observed_dealloc_size: usize::MAX,
            observed_dealloc_align: usize::MAX,
            policy_flags_seen: u32::MAX,
        }; 4];

        let rejected = unsafe {
            __unialloc_semantic_type_stats_snapshot_checked(
                rows.as_mut_ptr(),
                rows.len(),
                size_of::<SemanticTypeStatsSnapshot>() - 1,
            )
        };
        assert_eq!(rejected, 0);
        assert_eq!(rows[0].type_id, u64::MAX);

        let rejected_oversized = unsafe {
            __unialloc_semantic_type_stats_snapshot_checked(
                rows.as_mut_ptr(),
                rows.len(),
                size_of::<SemanticTypeStatsSnapshot>() + align_of::<usize>(),
            )
        };
        assert_eq!(
            rejected_oversized, 0,
            "checked row copies must reject a larger stride unless the implementation actually advances by that stride"
        );
        assert_eq!(rows[0].type_id, u64::MAX);

        let copied = unsafe {
            __unialloc_semantic_type_stats_snapshot_checked(
                rows.as_mut_ptr(),
                rows.len(),
                size_of::<SemanticTypeStatsSnapshot>(),
            )
        };
        assert!(copied >= 1);
        assert!(rows.iter().any(|row| {
            row.type_id == metadata.type_id
                && row.module_id == metadata.module_id
                && row.callsite == metadata.callsite
                && row.allocations == 1
                && row.deallocations == 1
        }));

        semantic_type_stats_recording_disable();
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_stats_record_dealloc_tracks_policy_flags_without_matching_alloc() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let metadata = AllocationMetadata::for_type(0xC002_DEA1)
            .with_module(0x5354_4154)
            .with_callsite(0xD1EC_A110)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED | FLAG_DELAYED_FREE);

        record_stats_dealloc(metadata);

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.total_deallocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(
            snap.policy_flags_seen & metadata.flags,
            metadata.flags,
            "aggregate coverage counters must preserve policy evidence from dealloc-only paths"
        );

        let mut rows = [empty_type_stats_row(); 4];
        let count = semantic_type_stats_snapshot(&mut rows);
        let row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == metadata.type_id
                    && row.module_id == metadata.module_id
                    && row.callsite == metadata.callsite
            })
            .copied()
            .expect("dealloc-only typed stats row");
        assert_eq!(row.allocations, 0);
        assert_eq!(row.deallocations, 1);
        assert_eq!(row.policy_flags_seen & metadata.flags, metadata.flags);
    }

    #[test]
    fn type_cache_reuses_matching_type() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(55);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(ptr, layout, metadata));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
            assert!(pop_type_cache(layout, metadata).is_none());
        }
    }

    #[test]
    fn type_cache_clears_corrupt_linked_slot_before_bypass() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let layout =
                Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_CACE);
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let slot_idx = type_cache_slot(slot_key);

            TYPE_CACHE[slot_idx] = TypeCacheSlot {
                cache_key: slot_key,
                type_id: metadata.type_id,
                head: 1usize as *mut u8,
                count: 1,
                retained_bytes: type_cache_retained_bytes_for_layout(layout),
            };
            remember_type_cache_hot_slot(slot_key, slot_idx);

            assert_eq!(pop_type_cache(layout, metadata), None);
            assert_eq!(TYPE_CACHE[slot_idx].cache_key, UNKNOWN_SEMANTIC_ID);
            assert_eq!(TYPE_CACHE[slot_idx].type_id, UNKNOWN_SEMANTIC_ID);
            assert!(TYPE_CACHE[slot_idx].head.is_null());
            assert_eq!(TYPE_CACHE[slot_idx].count, 0);
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );

            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let ptr = storage.as_mut_ptr() as *mut u8;
            assert!(push_type_cache(ptr, layout, metadata));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
        }
    }

    #[test]
    fn type_cache_clears_count_mismatch_without_reusing_stale_chain() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let layout =
                Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_CA17);
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let slot_idx = type_cache_slot(slot_key);
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];

            TYPE_CACHE[slot_idx] = TypeCacheSlot {
                cache_key: slot_key,
                type_id: metadata.type_id,
                head: storage.as_mut_ptr() as *mut u8,
                count: 0,
                retained_bytes: 0,
            };
            remember_type_cache_hot_slot(slot_key, slot_idx);

            assert_eq!(pop_type_cache(layout, metadata), None);
            assert_eq!(TYPE_CACHE[slot_idx].cache_key, UNKNOWN_SEMANTIC_ID);
            assert!(TYPE_CACHE[slot_idx].head.is_null());
            assert_eq!(TYPE_CACHE[slot_idx].count, 0);
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn type_cache_does_not_reuse_same_type_across_module_contexts() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let module_a = AllocationMetadata::for_type(0xC003_0048)
                .with_module(0xC0DE_A)
                .with_flags(FLAG_TYPE_ISOLATED);
            let module_b = AllocationMetadata::for_type(module_a.type_id)
                .with_module(0xC0DE_B)
                .with_flags(module_a.flags);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(ptr, layout, module_a));
            assert_eq!(
                pop_type_cache(layout, module_b),
                None,
                "type cache must not alias the same type id across module/context ids"
            );
            assert_eq!(pop_type_cache(layout, module_a), Some(ptr));
        }
    }

    #[test]
    fn semantic_type_cache_inline_round_trip_avoids_slot_table() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_0040);
            let ptr = storage.as_mut_ptr() as *mut u8;
            let alloc = RustAllocator::new();

            assert!(cache_semantic_free(&alloc, ptr, layout, metadata));
            let inline = inline_type_cache_entry_snapshot_for_test();
            assert_eq!(inline.ptr, ptr);
            assert_eq!(inline.type_id, metadata.type_id);
            assert!(
                type_cache_snapshot_for_test()
                    .iter()
                    .all(|slot| slot.head.is_null() && slot.count == 0),
                "single-live-object hot path should not touch the linked-list type cache"
            );

            assert_eq!(pop_semantic_type_cache(layout, metadata), Some(ptr));
            assert!(INLINE_TYPE_CACHE_ENTRY.is_empty());
        }
    }

    #[test]
    fn type_isolation_side_cache_snapshot_tracks_inline_entry() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let initial = type_isolation_side_cache_snapshot();
        assert!(!initial.inline_occupied);
        assert_eq!(initial.occupied_entries, 0);
        assert_eq!(initial.corrupt_slots, 0);

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5207)
            .with_module(0xC003)
            .with_callsite(0xA110_5207)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let after_free = type_isolation_side_cache_snapshot();
        assert!(
            after_free.inline_occupied,
            "single hot type-isolated free should be visible in the ordinary TLS side-cache snapshot"
        );
        assert_eq!(after_free.occupied_entries, 1);
        assert_eq!(after_free.occupied_slots, 0);
        assert_eq!(after_free.corrupt_slots, 0);

        let reused = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_eq!(reused, ptr);
        let after_reuse = type_isolation_side_cache_snapshot();
        assert!(!after_reuse.inline_occupied);
        unsafe {
            alloc.dealloc_raw(reused, layout);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn semantic_type_cache_inline_does_not_reuse_across_modules() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let module_a = AllocationMetadata::for_type(0xC003_0049)
                .with_module(0xC0DE_A)
                .with_flags(FLAG_TYPE_ISOLATED);
            let module_b = AllocationMetadata::for_type(module_a.type_id)
                .with_module(0xC0DE_B)
                .with_flags(module_a.flags);
            let ptr = storage.as_mut_ptr() as *mut u8;
            let alloc = RustAllocator::new();

            assert!(cache_semantic_free(&alloc, ptr, layout, module_a));
            assert_eq!(inline_type_cache_entry_snapshot_for_test().ptr, ptr);
            assert_eq!(pop_semantic_type_cache(layout, module_b), None);
            assert_eq!(pop_semantic_type_cache(layout, module_a), Some(ptr));
        }
    }

    #[test]
    fn semantic_allocator_does_not_reuse_cached_object_across_type_ids() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata_a = AllocationMetadata::for_type(0xC003_A11C)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED);
        let metadata_b = AllocationMetadata::for_type(0xC003_B11C)
            .with_module(metadata_a.module_id)
            .with_flags(metadata_a.flags);

        let ptr_a = unsafe { alloc.alloc_with_metadata(layout, metadata_a) };
        assert!(!ptr_a.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr_a, layout, metadata_a);
        }

        let ptr_b = unsafe { alloc.alloc_with_metadata(layout, metadata_b) };
        assert!(!ptr_b.is_null());
        assert_ne!(
            ptr_b, ptr_a,
            "a different compiler object type must not consume a cached object from type A"
        );

        let ptr_a_reused = unsafe { alloc.alloc_with_metadata(layout, metadata_a) };
        assert_eq!(
            ptr_a_reused, ptr_a,
            "the cached object should remain available to the original compiler object type"
        );

        unsafe {
            alloc.dealloc_raw(ptr_b, layout);
            alloc.dealloc_raw(ptr_a_reused, layout);
            clear_type_cache_for_test();
            clear_auto_allocation_records();
        }
    }

    #[test]
    fn semantic_type_cache_inline_mismatch_keeps_entry_recoverable() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let small =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let large = Layout::from_size_align(small.size() * 2, align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_0042);
            let ptr = storage.as_mut_ptr() as *mut u8;
            let alloc = RustAllocator::new();

            assert!(cache_semantic_free(&alloc, ptr, small, metadata));
            assert_eq!(pop_semantic_type_cache(large, metadata), None);
            assert_eq!(pop_semantic_type_cache(small, metadata), Some(ptr));
        }
    }

    #[test]
    fn compiler_type_recovery_fast_path_records_and_reuses_inline_cache() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_0043)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C343)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert_eq!(take_auto_deallocation_metadata(ptr, layout), Some(metadata));
        unsafe {
            alloc.dealloc_with_recovered_metadata(ptr, layout, metadata);
        }
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, ptr);

        let reused = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert_eq!(
            reused, ptr,
            "the compiler TYPE_ISOLATED-only recovery fast path should reuse the inline type cache"
        );
        assert_eq!(
            take_auto_deallocation_metadata(reused, layout),
            Some(metadata)
        );
        unsafe {
            alloc.dealloc_raw(reused, layout);
            clear_auto_allocation_records();
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn compiler_recovery_allocation_fail_closes_when_record_tables_full() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_004A)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C34A)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let probe = unsafe { alloc.alloc_raw(layout) };
        assert!(
            !probe.is_null(),
            "fixed-heap allocator must be live before forcing record-table exhaustion"
        );
        unsafe {
            alloc.dealloc_raw(probe, layout);
            fill_auto_allocation_record_tables_for_test(layout, metadata);
        }

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(
            ptr.is_null(),
            "compiler recovery allocations must fail closed instead of returning a pointer with no exact deallocation record"
        );
        clear_auto_allocation_records();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn compiler_recovery_realloc_in_place_fails_when_new_record_cannot_be_installed() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_size = (MIN_TYPE_CACHE_OBJECT_SIZE..crate::size_class::MAX_SIZE)
            .find(|&size| {
                crate::size_class::get_size_class(size).index()
                    == crate::size_class::get_size_class(size + 1).index()
            })
            .expect("fixed-heap test needs a same-size-class growth request");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_size = old_layout.size() + 1;
        let metadata = AllocationMetadata::for_type(0xC003_004C)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C34C)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert!(
            semantic_realloc_can_reuse_in_place(old_layout, new_size, metadata, metadata),
            "test must exercise the in-place realloc fast path"
        );

        let ptr = unsafe { alloc.alloc_raw(old_layout) };
        assert!(
            !ptr.is_null(),
            "fixed-heap allocator must be live before forcing record-table exhaustion"
        );
        unsafe {
            fill_auto_allocation_record_tables_for_test(old_layout, metadata);
        }

        let grown = with_auto_allocation_recovery_recording(|| unsafe {
            alloc.realloc_with_split_metadata(ptr, old_layout, new_size, metadata, metadata)
        });
        assert!(
            grown.is_null(),
            "in-place compiler recovery realloc must fail closed when the updated exact metadata record cannot be installed"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            None,
            "failed in-place realloc should not install a bogus old-layout recovery record for this pointer"
        );
        unsafe {
            clear_auto_allocation_records();
            alloc.dealloc_raw(ptr, old_layout);
        }
    }

    #[test]
    fn compiler_type_metadata_fast_path_without_recovery_avoids_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_0044)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C344)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "plain SemanticAlloc metadata calls should not create recovery records"
        );

        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, ptr);
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "explicit metadata deallocation should leave no recovery record behind"
        );

        let reused = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert_eq!(
            reused, ptr,
            "explicit metadata fast path should reuse the inline type cache without recovery bookkeeping"
        );
        unsafe {
            alloc.dealloc_raw(reused, layout);
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn compiler_type_metadata_fast_path_records_stats_when_enabled() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_5147)
            .with_module(0xC0DE)
            .with_callsite(0xA110_5147)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert!(semantic_stats_any_recording_enabled());
        assert!(
            compiler_type_isolated_recovery_fast_path(metadata),
            "stats are event accounting only and should not eject exact compiler metadata from the cache/recovery fast path"
        );

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "same-thread compiler recovery should stay out of the global recovery table even while stats are enabled"
        );
        assert!(current_thread_fast_auto_allocation_records_active());

        unsafe {
            RustAllocator::new().dealloc(ptr, layout);
        }
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, ptr);

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.total_deallocations, 1);
        assert_eq!(snap.typed_allocated_bytes, layout.size());

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }; 4];
        let row_count = semantic_type_stats_snapshot(&mut rows);
        assert_eq!(row_count, 1);
        let row = rows[0];
        assert_eq!(row.type_id, metadata.type_id);
        assert_eq!(row.module_id, metadata.module_id);
        assert_eq!(row.callsite, metadata.callsite);
        assert_eq!(row.allocations, 1);
        assert_eq!(row.deallocations, 1);
        assert_eq!(row.allocated_bytes, layout.size());

        unsafe {
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[test]
    fn compiler_type_metadata_fast_path_allows_cache_only_side_table_policies() {
        let _guard = test_guard();
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let base = AllocationMetadata::for_type(0xC003_4A51)
            .with_module(0xC0DE)
            .with_callsite(0xA110_4A51);
        for flags in [
            FLAG_TYPE_ISOLATED,
            FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
            FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA,
            FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH,
            FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION,
            FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED | FLAG_METADATA_PROTECTION,
        ] {
            assert!(
                compiler_type_isolated_recovery_fast_path(base.with_flags(flags)),
                "cache-only compiler policy flags {:#x} should stay on the exact metadata fast path",
                flags
            );
        }

        for side_effect_flag in [
            FLAG_FORCE_INITIALIZE,
            FLAG_DELAYED_FREE,
            FLAG_GUARD_PAGES,
            FLAG_MEMORY_TAGGING,
        ] {
            assert!(
                !compiler_type_isolated_recovery_fast_path(
                    base.with_flags(FLAG_TYPE_ISOLATED | side_effect_flag)
                ),
                "policy flag {:#x} needs additional allocator hooks and must not use the cache-only fast path",
                side_effect_flag
            );
        }

        assert!(
            !compiler_type_isolated_recovery_fast_path(AllocationMetadata {
                placement_hint: LAYOUT_DERIVED_PLACEMENT_HINT,
                ..base
            }),
            "layout-derived smoke metadata must not be promoted to the exact compiler fast path"
        );
    }

    #[test]
    fn local_compiler_side_table_fast_path_reuses_without_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_4A52)
            .with_module(0xC0DE)
            .with_callsite(0xA110_4A52)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED)
            .with_lifetime_hint(0x52)
            .with_placement_hint(0x21);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe {
            __unialloc_alloc_layout_with_metadata_hints_local(
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "local metadata-segregated compiler ABI must not create recovery records"
        );

        unsafe {
            __unialloc_dealloc_layout_with_metadata_hints_local(
                ptr,
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            );
        }
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        assert!(
            unsafe { cached_segregated_type_cache_entry_for_test(layout, metadata, ptr).is_some() },
            "metadata-segregated local deallocation should feed the side cache, not the plain raw path"
        );

        let reused = unsafe {
            __unialloc_alloc_layout_with_metadata_hints_local(
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert_eq!(
            reused, ptr,
            "cache-only metadata-segregated fast path should reuse the side-cache entry"
        );
        unsafe {
            RustAllocator::new().dealloc_raw(reused, layout);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn ffi_local_compiler_type_metadata_fast_path_avoids_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_0144)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C144)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_lifetime_hint(0x11)
            .with_placement_hint(0x22);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe {
            __unialloc_alloc_layout_with_metadata_hints_local(
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "local compiler ABI assumes paired metadata deallocation and must not create recovery records"
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "local compiler ABI must not arm ordinary GlobalAlloc recovery slow path"
        );

        unsafe {
            __unialloc_dealloc_layout_with_metadata_hints(
                ptr,
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            );
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "paired metadata deallocation should leave no recovery record behind"
        );
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, ptr);

        let reused = unsafe {
            __unialloc_alloc_with_metadata_hints_local(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert_eq!(
            reused, ptr,
            "local compiler ABI should still reuse the type-isolated fast cache"
        );
        unsafe {
            RustAllocator::new().dealloc_raw(reused, layout);
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[test]
    fn ffi_local_compiler_dealloc_avoids_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_0146)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C146)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_lifetime_hint(0x55)
            .with_placement_hint(0x66);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe {
            __unialloc_alloc_layout_with_metadata_hints_local(
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);

        unsafe {
            __unialloc_dealloc_layout_with_metadata_hints_local(
                ptr,
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            );
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "local layout dealloc must not consult or create recovery records"
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "local compiler dealloc must not arm ordinary recovery slow path"
        );
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, ptr);

        let reused = unsafe {
            __unialloc_alloc_with_metadata_hints_local(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert_eq!(
            reused, ptr,
            "local dealloc should feed the type-isolated fast cache"
        );

        assert!(unsafe {
            __unialloc_dealloc_with_metadata_hints_local(
                reused,
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        });
        assert_eq!(
            lookup_auto_allocation_metadata(reused, layout),
            None,
            "local C dealloc ABI must also leave no recovery record"
        );
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, reused);

        let final_ptr = unsafe {
            __unialloc_alloc_layout_with_metadata_hints_local(
                layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert_eq!(final_ptr, ptr);
        unsafe {
            RustAllocator::new().dealloc_raw(final_ptr, layout);
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[test]
    fn ffi_local_compiler_realloc_consumes_old_record_without_new_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires same-size-class growth");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_0145)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C145)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_lifetime_hint(0x33)
            .with_placement_hint(0x44);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));
        assert!(semantic_realloc_can_reuse_in_place(
            old_layout, new_size, metadata, metadata
        ));

        let ptr = unsafe {
            __unialloc_alloc_layout_with_metadata_hints(
                old_layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            Some(metadata),
            "conservative compiler ABI keeps the old record until local realloc consumes it"
        );

        let grown = unsafe {
            __unialloc_realloc_layout_with_metadata_hints_local(
                ptr,
                old_layout,
                new_size,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert_eq!(
            grown, ptr,
            "same-size-class local compiler realloc should stay on the in-place fast path"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            None,
            "local realloc must consume the old conservative recovery record"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(grown, new_layout),
            None,
            "local realloc must not install a new recovery record"
        );
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "after local realloc consumes the only old record, ordinary runtime slow path should clear"
        );

        unsafe {
            __unialloc_dealloc_layout_with_metadata_hints(
                grown,
                new_layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            );
        }
        let reused = unsafe {
            __unialloc_alloc_layout_with_metadata_hints_local(
                new_layout,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert_eq!(
            reused, grown,
            "paired exact dealloc after local realloc should still feed the type cache"
        );
        unsafe {
            RustAllocator::new().dealloc_raw(reused, new_layout);
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[test]
    fn ffi_compiler_type_metadata_fast_dealloc_consumes_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_0045)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C345)
            .with_flags(FLAG_TYPE_ISOLATED);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(metadata),
            "FFI allocation ABI keeps a recovery record for ordinary unwritten drops"
        );

        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                ptr,
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        });
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "FFI metadata deallocation must consume the allocation recovery record"
        );
        assert_eq!(unsafe { INLINE_TYPE_CACHE_ENTRY.ptr }, ptr);

        let reused = unsafe {
            __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert_eq!(reused, ptr);
        assert_eq!(
            lookup_auto_allocation_metadata(reused, layout),
            Some(metadata)
        );
        unsafe {
            RustAllocator::new().dealloc_raw(reused, layout);
            clear_auto_allocation_records();
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[test]
    fn ffi_dealloc_mismatched_type_uses_recovered_allocation_identity_for_cache() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let allocation_metadata = AllocationMetadata::for_type(0xC003_004B)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C34B)
            .with_flags(FLAG_TYPE_ISOLATED);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(0xC003_BADB)
            .with_module(0xC0DE)
            .with_callsite(0xD0D0_BADB)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                allocation_metadata.type_id,
                allocation_metadata.module_id,
                allocation_metadata.flags,
                allocation_metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(allocation_metadata)
        );

        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                ptr,
                layout.size(),
                layout.align(),
                wrong_dealloc_metadata.type_id,
                wrong_dealloc_metadata.module_id,
                wrong_dealloc_metadata.flags,
                wrong_dealloc_metadata.callsite,
            )
        });
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "the exact allocation recovery record should still be consumed once"
        );
        assert_eq!(
            unsafe { pop_semantic_type_cache(layout, wrong_dealloc_metadata) },
            None,
            "mismatched explicit deallocation metadata must not poison the type cache"
        );
        assert_eq!(
            unsafe { pop_semantic_type_cache(layout, allocation_metadata) },
            Some(ptr),
            "the object should remain cached under its recovered allocation-site identity"
        );
        unsafe {
            RustAllocator::new().dealloc_raw(ptr, layout);
            clear_auto_allocation_records();
            INLINE_TYPE_CACHE_ENTRY = InlineTypeCacheEntry::empty();
        }
    }

    #[test]
    fn ffi_dealloc_mismatched_metadata_segregated_type_uses_recovered_identity() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout = Layout::from_size_align(4 * size_of::<usize>(), align_of::<usize>()).unwrap();
        let allocation_metadata = AllocationMetadata::for_type(0x5E6D_70C1)
            .with_module(0xC0DE)
            .with_callsite(0xA110_70C1)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
        let wrong_dealloc_metadata = AllocationMetadata::for_type(0x5E6D_BAD1)
            .with_module(0xC0DE)
            .with_callsite(0xD0D0_BAD1)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                layout.size(),
                layout.align(),
                allocation_metadata.type_id,
                allocation_metadata.module_id,
                allocation_metadata.flags,
                allocation_metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(allocation_metadata)
        );

        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                ptr,
                layout.size(),
                layout.align(),
                wrong_dealloc_metadata.type_id,
                wrong_dealloc_metadata.module_id,
                wrong_dealloc_metadata.flags,
                wrong_dealloc_metadata.callsite,
            )
        });
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "side-table deallocation must consume the exact allocation recovery record"
        );
        assert_eq!(
            unsafe { pop_semantic_type_cache(layout, wrong_dealloc_metadata) },
            None,
            "mismatched explicit metadata must not poison the metadata-segregated cache"
        );
        assert_eq!(
            unsafe { pop_semantic_type_cache(layout, allocation_metadata) },
            Some(ptr),
            "metadata-segregated cache should use the recovered allocation identity"
        );
        unsafe {
            RustAllocator::new().dealloc_raw(ptr, layout);
            clear_auto_allocation_records();
        }
    }

    #[test]
    fn hinted_compiler_recovery_metadata_survives_cross_thread_dealloc() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC003_8042)
            .with_module(0xC0DE)
            .with_callsite(0xA110_8042)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_placement_hint(PLACEMENT_HINT_CROSS_THREAD_RECOVERY | 0x42);
        assert!(compiler_type_isolated_recovery_fast_path(metadata));

        let ptr = unsafe {
            __unialloc_alloc_with_metadata_hints(
                layout.size(),
                layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.lifetime_hint,
                metadata.placement_hint,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(metadata),
            "cross-thread compiler hint should publish recovery metadata process-wide"
        );
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            1,
            "hinted escaping allocations use the global recovery table, not the same-thread TLS table"
        );
        assert!(!current_thread_fast_auto_allocation_records_active());
        assert!(
            semantic_runtime_slow_path_enabled(),
            "global recovery record must wake ordinary deallocation on a foreign thread"
        );

        let ptr_addr = ptr as usize;
        let worker = thread::spawn(move || {
            let alloc = RustAllocator::new();
            let ptr = ptr_addr as *mut u8;
            assert!(
                semantic_runtime_slow_path_enabled(),
                "foreign thread should see the process-visible recovery record"
            );
            unsafe {
                alloc.dealloc(ptr, layout);
            }
            assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);

            let cached =
                unsafe { pop_semantic_type_cache(layout, metadata).expect("typed free cached") };
            assert_eq!(cached, ptr);
            unsafe {
                alloc.dealloc_raw(cached, layout);
            }
        });
        worker.join().expect("cross-thread semantic deallocation");

        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        assert!(
            !semantic_runtime_slow_path_enabled(),
            "cross-thread drop should consume the final global recovery record"
        );
    }
    #[cfg(feature = "stats")]
    #[test]
    fn global_dealloc_prefers_compatible_active_drop_metadata_over_recovery_record() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let allocation_metadata = AllocationMetadata::for_type(0xC003_0046)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C346)
            .with_flags(FLAG_TYPE_ISOLATED);
        let drop_metadata = AllocationMetadata::for_type(allocation_metadata.type_id)
            .with_module(allocation_metadata.module_id)
            .with_callsite(0xD0D0_C346)
            .with_flags(allocation_metadata.flags);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, allocation_metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(allocation_metadata)
        );

        let previous = unsafe { set_active_metadata(drop_metadata) };
        unsafe {
            alloc.dealloc(ptr, layout);
            restore_active_metadata(previous);
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "active compiler Drop metadata should still consume the allocation recovery record"
        );

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }; 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let allocation_row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == allocation_metadata.type_id
                    && row.callsite == allocation_metadata.callsite
            })
            .expect("allocation-site type stats row");
        let drop_row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == drop_metadata.type_id && row.callsite == drop_metadata.callsite
            })
            .expect("drop-site type stats row");
        assert_eq!(allocation_row.allocations, 1);
        assert_eq!(
            allocation_row.deallocations, 0,
            "compatible active Drop metadata should own the deallocation accounting"
        );
        assert_eq!(drop_row.allocations, 0);
        assert_eq!(drop_row.deallocations, 1);

        unsafe {
            let cached = pop_semantic_type_cache(layout, drop_metadata).expect("cached drop ptr");
            alloc.dealloc_raw(cached, layout);
            clear_auto_allocation_records();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_stats_recording_disable();
    }
    #[cfg(feature = "stats")]
    #[test]
    fn global_dealloc_keeps_recovery_record_when_active_metadata_mismatches() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, align_of::<usize>()).unwrap();
        let allocation_metadata = AllocationMetadata::for_type(0xC003_0047)
            .with_module(0xC0DE)
            .with_callsite(0xA110_C347)
            .with_flags(FLAG_TYPE_ISOLATED);
        let unrelated_active_metadata = AllocationMetadata::for_type(0xBAD0_C347)
            .with_module(allocation_metadata.module_id)
            .with_callsite(0xD0D0_C347)
            .with_flags(allocation_metadata.flags);

        let ptr = unsafe { alloc.alloc_with_recovery_metadata(layout, allocation_metadata) };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            Some(allocation_metadata)
        );

        let previous = unsafe { set_active_metadata(unrelated_active_metadata) };
        unsafe {
            alloc.dealloc(ptr, layout);
            restore_active_metadata(previous);
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, layout),
            None,
            "recorded metadata path should consume the record even when an unrelated scope is active"
        );

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }; 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let allocation_row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == allocation_metadata.type_id
                    && row.callsite == allocation_metadata.callsite
            })
            .expect("allocation-site type stats row");
        assert_eq!(allocation_row.allocations, 1);
        assert_eq!(
            allocation_row.deallocations, 1,
            "mismatched active metadata must not steal deallocation accounting"
        );
        assert!(rows.iter().take(count).all(|row| {
            row.type_id != unrelated_active_metadata.type_id || row.deallocations == 0
        }));

        unsafe {
            let cached =
                pop_semantic_type_cache(layout, allocation_metadata).expect("cached recorded ptr");
            alloc.dealloc_raw(cached, layout);
            clear_auto_allocation_records();
            restore_active_metadata(AllocationMetadata::unknown());
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn type_cache_hot_slot_tracks_repeated_type() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_0041);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(ptr, layout, metadata));
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let hot_slot = type_cache_hot_slot_snapshot_for_test();
            assert_eq!(hot_slot.cache_key, slot_key);
            let hot_idx = hot_slot.slot_idx;
            assert!(hot_idx < TYPE_CACHE_SLOTS);
            let hot_cache_slot = type_cache_slot_snapshot_for_test(hot_idx);
            assert_eq!(hot_cache_slot.cache_key, slot_key);
            assert_eq!(hot_cache_slot.type_id, metadata.type_id);

            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
            assert!(
                type_cache_hot_slot(slot_key).is_none(),
                "emptying a slot must clear the stale hot hint instead of rechecking it later"
            );
        }
    }

    #[test]
    fn type_cache_pop_emptying_slot_clears_stale_next_head() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut head_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut stale_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&head_storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_CA0E);
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let idx = type_cache_slot(slot_key);
            let head = head_storage.as_mut_ptr() as *mut u8;
            let stale_next = stale_storage.as_mut_ptr() as *mut u8;

            let node = head as *mut usize;
            node.write(stale_next as usize);
            node.add(1).write(layout.size());
            node.add(2).write(layout.align());
            TYPE_CACHE[idx] = TypeCacheSlot {
                cache_key: slot_key,
                type_id: metadata.type_id,
                head,
                count: 1,
                retained_bytes: type_cache_retained_bytes_for_layout(layout),
            };
            remember_type_cache_hot_slot(slot_key, idx);

            assert_eq!(pop_type_cache(layout, metadata), Some(head));
            assert_eq!(TYPE_CACHE[idx].cache_key, UNKNOWN_SEMANTIC_ID);
            assert_eq!(TYPE_CACHE[idx].type_id, UNKNOWN_SEMANTIC_ID);
            assert!(TYPE_CACHE[idx].head.is_null());
            assert_eq!(TYPE_CACHE[idx].count, 0);
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn type_cache_pop_clears_short_chain_after_returning_head() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_CA0F);
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let idx = type_cache_slot(slot_key);
            let ptr = storage.as_mut_ptr() as *mut u8;

            let node = ptr as *mut usize;
            node.write(core::ptr::null::<u8>() as usize);
            node.add(1).write(layout.size());
            node.add(2).write(layout.align());
            TYPE_CACHE[idx] = TypeCacheSlot {
                cache_key: slot_key,
                type_id: metadata.type_id,
                head: ptr,
                count: 2,
                retained_bytes: type_cache_retained_bytes_for_layout(layout) * 2,
            };
            remember_type_cache_hot_slot(slot_key, idx);

            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
            assert_eq!(TYPE_CACHE[idx].cache_key, UNKNOWN_SEMANTIC_ID);
            assert!(TYPE_CACHE[idx].head.is_null());
            assert_eq!(TYPE_CACHE[idx].count, 0);
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn type_cache_pop_clears_corrupt_count_before_traversal() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_CA11);
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let idx = type_cache_slot(slot_key);
            let ptr = storage.as_mut_ptr() as *mut u8;

            TYPE_CACHE[idx] = TypeCacheSlot {
                cache_key: slot_key,
                type_id: metadata.type_id,
                head: ptr,
                count: MAX_TYPE_CACHE_DEPTH + 1,
                retained_bytes: type_cache_retained_bytes_for_layout(layout),
            };
            remember_type_cache_hot_slot(slot_key, idx);

            assert_eq!(pop_type_cache(layout, metadata), None);
            assert_eq!(TYPE_CACHE[idx].cache_key, UNKNOWN_SEMANTIC_ID);
            assert_eq!(TYPE_CACHE[idx].head, core::ptr::null_mut());
            assert_eq!(TYPE_CACHE[idx].count, 0);
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn type_cache_push_recovers_corrupt_null_head_count() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xC003_CA12);
            let cache_key = type_cache_identity_key(metadata);
            let slot_key = plain_type_cache_slot_key(cache_key, layout);
            let idx = type_cache_slot(slot_key);
            let ptr = storage.as_mut_ptr() as *mut u8;

            TYPE_CACHE[idx] = TypeCacheSlot {
                cache_key: slot_key,
                type_id: metadata.type_id,
                head: core::ptr::null_mut(),
                count: 1,
                retained_bytes: type_cache_retained_bytes_for_layout(layout),
            };
            remember_type_cache_hot_slot(slot_key, idx);

            assert!(
                push_type_cache(ptr, layout, metadata),
                "push should clear stale null-head state instead of incrementing a corrupt count"
            );
            assert_eq!(TYPE_CACHE[idx].cache_key, slot_key);
            assert_eq!(TYPE_CACHE[idx].type_id, metadata.type_id);
            assert_eq!(TYPE_CACHE[idx].head, ptr);
            assert_eq!(TYPE_CACHE[idx].count, 1);
            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
        }
    }

    #[test]
    fn type_cache_handles_bounded_type_collision_probe() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut base_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut colliding_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let layout =
                Layout::from_size_align(size_of_val(&base_storage), align_of::<usize>()).unwrap();
            let base_ptr = base_storage.as_mut_ptr() as *mut u8;
            let colliding_ptr = colliding_storage.as_mut_ptr() as *mut u8;
            let base = AllocationMetadata::for_type(7);
            let primary = type_cache_slot(plain_type_cache_slot_key(
                type_cache_identity_key(base),
                layout,
            ));
            let colliding = (1..4096u64)
                .map(|delta| AllocationMetadata::for_type(base.type_id.wrapping_add(delta)))
                .find(|metadata| {
                    type_cache_slot(plain_type_cache_slot_key(
                        type_cache_identity_key(*metadata),
                        layout,
                    )) == primary
                })
                .expect("bounded search should find same type-cache bucket");
            assert_ne!(
                type_cache_identity_key(base),
                type_cache_identity_key(colliding)
            );

            assert!(push_type_cache(base_ptr, layout, base));
            assert!(push_type_cache(colliding_ptr, layout, colliding));
            assert_eq!(pop_type_cache(layout, colliding), Some(colliding_ptr));
            assert_eq!(pop_type_cache(layout, base), Some(base_ptr));
        }
    }

    #[test]
    fn type_cache_keeps_entry_on_layout_mismatch() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let small =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let large = Layout::from_size_align(small.size() * 2, align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(88);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(ptr, small, metadata));
            assert_eq!(pop_type_cache(large, metadata), None);
            assert_eq!(pop_type_cache(small, metadata), Some(ptr));
        }
    }

    #[test]
    fn type_cache_finds_matching_layout_behind_mismatch() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut small_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut large_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let small =
                Layout::from_size_align(size_of_val(&small_storage), align_of::<usize>()).unwrap();
            let large =
                Layout::from_size_align(size_of_val(&large_storage), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(90);
            let small_ptr = small_storage.as_mut_ptr() as *mut u8;
            let large_ptr = large_storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(small_ptr, small, metadata));
            assert!(push_type_cache(large_ptr, large, metadata));
            assert_eq!(pop_type_cache(small, metadata), Some(small_ptr));
            assert_eq!(pop_type_cache(large, metadata), Some(large_ptr));
        }
    }

    #[test]
    fn type_cache_rejects_larger_cached_block_for_smaller_layout() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let large =
                Layout::from_size_align(size_of_val(&storage), align_of::<usize>()).unwrap();
            let small = Layout::from_size_align(
                TYPE_CACHE_NODE_WORDS * core::mem::size_of::<usize>(),
                align_of::<usize>(),
            )
            .unwrap();
            let metadata = AllocationMetadata::for_type(89);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(ptr, large, metadata));
            assert_eq!(pop_type_cache(small, metadata), None);
            assert_eq!(pop_type_cache(large, metadata), Some(ptr));
        }
    }

    #[test]
    fn type_cache_retained_bytes_counter_tracks_per_layout_push_and_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut small_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut large_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let small =
                Layout::from_size_align(size_of_val(&small_storage), align_of::<usize>()).unwrap();
            let large =
                Layout::from_size_align(size_of_val(&large_storage), align_of::<usize>()).unwrap();
            let small_retained = type_cache_retained_bytes_for_layout(small);
            let large_retained = type_cache_retained_bytes_for_layout(large);
            let metadata = AllocationMetadata::for_type(0xC003_5207).with_flags(FLAG_TYPE_ISOLATED);
            let small_ptr = small_storage.as_mut_ptr() as *mut u8;
            let large_ptr = large_storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(small_ptr, small, metadata));
            let cache_key = type_cache_identity_key(metadata);
            let small_slot_key = plain_type_cache_slot_key(cache_key, small);
            let large_slot_key = plain_type_cache_slot_key(cache_key, large);
            assert_ne!(
                small_slot_key, large_slot_key,
                "plain cold-cache slots are layout-qualified to avoid mixing sizes"
            );
            let slot = type_cache_find_slot_for_key(small_slot_key, false)
                .expect("slot should exist after the first push");
            assert_eq!((*slot).count, 1);
            assert_eq!((*slot).retained_bytes, small_retained);
            assert_eq!(type_cache_slot_retained_bytes(&*slot), Some(small_retained));

            assert!(push_type_cache(large_ptr, large, metadata));
            let small_slot = type_cache_find_slot_for_key(small_slot_key, false)
                .expect("small-layout slot should remain after the large push");
            assert_eq!((*small_slot).count, 1);
            assert_eq!((*small_slot).retained_bytes, small_retained);
            let large_slot = type_cache_find_slot_for_key(large_slot_key, false)
                .expect("large-layout slot should exist after the large push");
            assert_eq!((*large_slot).count, 1);
            assert_eq!((*large_slot).retained_bytes, large_retained);

            assert_eq!(pop_type_cache(small, metadata), Some(small_ptr));
            assert!(
                type_cache_find_slot_for_key(small_slot_key, false).is_none(),
                "emptying the small-layout slot should not clear the large-layout slot"
            );
            let slot = type_cache_find_slot_for_key(large_slot_key, false)
                .expect("large entry should remain after popping the tail small entry");
            assert_eq!((*slot).count, 1);
            assert_eq!((*slot).retained_bytes, large_retained);
            assert_eq!(type_cache_slot_retained_bytes(&*slot), Some(large_retained));

            assert_eq!(pop_type_cache(large, metadata), Some(large_ptr));
            assert!(type_cache_find_slot_for_key(large_slot_key, false).is_none());
            assert_eq!(
                type_cache_hot_slot_snapshot_for_test().cache_key,
                UNKNOWN_SEMANTIC_ID
            );
        }
    }

    #[test]
    fn type_cache_retained_bytes_use_allocator_rounded_size() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS + 4];
            let layout = first_unrounded_type_cache_layout_for_test();
            assert!(
                layout.size() <= size_of_val(&storage),
                "test storage must cover the requested unrounded layout"
            );
            let retained = type_cache_retained_bytes_for_layout(layout);
            assert!(
                retained > layout.size(),
                "test layout must exercise internal-fragmentation accounting"
            );
            let metadata = AllocationMetadata::for_type(0xC003_5211).with_flags(FLAG_TYPE_ISOLATED);
            let ptr = storage.as_mut_ptr() as *mut u8;

            assert!(push_type_cache(ptr, layout, metadata));
            let cache_key = type_cache_identity_key(metadata);
            let slot =
                type_cache_find_slot_for_key(plain_type_cache_slot_key(cache_key, layout), false)
                    .expect("slot should exist after push");
            assert_eq!((*slot).count, 1);
            assert_eq!((*slot).retained_bytes, retained);
            assert_eq!(type_cache_slot_retained_bytes(&*slot), Some(retained));

            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
            assert!(type_cache_find_slot_for_key(
                plain_type_cache_slot_key(cache_key, layout),
                false
            )
            .is_none());
        }
    }

    #[test]
    fn type_cache_slot_byte_budget_rejects_cold_overflow_entry() {
        const QUARTER_SLOT_WORDS: usize =
            (MAX_TYPE_CACHE_SLOT_BYTES / 4) / core::mem::size_of::<usize>();

        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let mut first = [0usize; QUARTER_SLOT_WORDS];
            let mut second = [0usize; QUARTER_SLOT_WORDS];
            let mut third = [0usize; QUARTER_SLOT_WORDS];
            let mut fourth = [0usize; QUARTER_SLOT_WORDS];
            let mut rejected = [0usize; QUARTER_SLOT_WORDS];
            let layout = Layout::from_size_align(size_of_val(&first), align_of::<usize>()).unwrap();
            assert_eq!(layout.size() * 4, MAX_TYPE_CACHE_SLOT_BYTES);
            assert!(layout.size() >= MIN_TYPE_CACHE_OBJECT_SIZE);

            let metadata = AllocationMetadata::for_type(0xC003_5208).with_flags(FLAG_TYPE_ISOLATED);
            let ptrs = [
                first.as_mut_ptr() as *mut u8,
                second.as_mut_ptr() as *mut u8,
                third.as_mut_ptr() as *mut u8,
                fourth.as_mut_ptr() as *mut u8,
            ];
            for &ptr in ptrs.iter() {
                assert!(push_type_cache(ptr, layout, metadata));
            }

            let cache_key = type_cache_identity_key(metadata);
            let slot =
                type_cache_find_slot_for_key(plain_type_cache_slot_key(cache_key, layout), false)
                    .expect("slot should exist after four pushes");
            assert_eq!(
                type_cache_slot_retained_bytes(&*slot),
                Some(MAX_TYPE_CACHE_SLOT_BYTES)
            );
            assert_eq!((*slot).retained_bytes, MAX_TYPE_CACHE_SLOT_BYTES);
            assert_eq!((*slot).count, 4);

            assert!(
                !push_type_cache(rejected.as_mut_ptr() as *mut u8, layout, metadata),
                "a fifth 16KiB object would exceed the 64KiB cold-slot byte budget"
            );
            let slot =
                type_cache_find_slot_for_key(plain_type_cache_slot_key(cache_key, layout), false)
                    .expect("rejected push must not clear existing valid cache entries");
            assert_eq!(
                type_cache_slot_retained_bytes(&*slot),
                Some(MAX_TYPE_CACHE_SLOT_BYTES)
            );
            assert_eq!((*slot).retained_bytes, MAX_TYPE_CACHE_SLOT_BYTES);
            assert_eq!((*slot).count, 4);

            assert_eq!(pop_type_cache(layout, metadata), Some(ptrs[3]));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptrs[2]));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptrs[1]));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptrs[0]));
            assert_eq!(pop_type_cache(layout, metadata), None);
        }
    }

    #[test]
    fn type_cache_aggregate_byte_budget_rejects_cross_slot_overflow() {
        const QUARTER_SLOT_WORDS: usize =
            (MAX_TYPE_CACHE_SLOT_BYTES / 4) / core::mem::size_of::<usize>();

        let _guard = test_guard();
        let mut storage: Vec<Box<[usize; QUARTER_SLOT_WORDS]>> = Vec::new();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();

            let layout = Layout::from_size_align(
                QUARTER_SLOT_WORDS * core::mem::size_of::<usize>(),
                align_of::<usize>(),
            )
            .unwrap();
            let retained = type_cache_retained_bytes_for_layout(layout);
            assert!(retained > 0);
            assert!(retained <= MAX_TYPE_CACHE_SLOT_BYTES);
            let fit_entries = MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES / retained;
            assert!(
                fit_entries > 1 && fit_entries < TYPE_CACHE_SLOTS * MAX_TYPE_CACHE_DEPTH,
                "test must fill the aggregate cache budget before table capacity"
            );

            let mut accepted = 0usize;
            let mut next_type = 0xC003_5300u64;
            let mut attempts = 0usize;
            while accepted < fit_entries {
                assert!(
                    attempts < 16_384,
                    "test should find enough insertable cache keys"
                );
                storage.push(Box::new([0usize; QUARTER_SLOT_WORDS]));
                let ptr = storage.last_mut().unwrap().as_mut_ptr() as *mut u8;
                let metadata =
                    AllocationMetadata::for_type(next_type).with_flags(FLAG_TYPE_ISOLATED);
                next_type = next_type.wrapping_add(1);
                attempts += 1;
                if push_type_cache(ptr, layout, metadata) {
                    accepted += 1;
                } else {
                    storage.pop();
                }
            }

            let before_overflow = type_isolation_side_cache_snapshot();
            assert!(
                before_overflow.retained_bytes <= MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES,
                "accepted entries must stay within the aggregate budget"
            );
            assert!(
                before_overflow.retained_bytes.saturating_add(retained)
                    > MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES,
                "one more object must overflow the aggregate budget"
            );

            let mut overflow_accepted = false;
            let mut overflow_attempts = 0usize;
            while overflow_attempts < 16_384 {
                storage.push(Box::new([0usize; QUARTER_SLOT_WORDS]));
                let ptr = storage.last_mut().unwrap().as_mut_ptr() as *mut u8;
                let metadata =
                    AllocationMetadata::for_type(next_type).with_flags(FLAG_TYPE_ISOLATED);
                next_type = next_type.wrapping_add(1);
                overflow_attempts += 1;
                let before = type_isolation_side_cache_snapshot().retained_bytes;
                if push_type_cache(ptr, layout, metadata) {
                    overflow_accepted = true;
                    break;
                }
                storage.pop();
                assert_eq!(
                    type_isolation_side_cache_snapshot().retained_bytes,
                    before,
                    "a rejected aggregate-budget push must not mutate retained bytes"
                );
            }

            assert!(
                !overflow_accepted,
                "plain semantic type-cache retention must be capped across distinct slots"
            );
            assert!(
                type_isolation_side_cache_snapshot().retained_bytes
                    <= MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES
            );
        }
    }

    #[test]
    fn segregated_type_cache_retained_bytes_counter_tracks_push_and_pop() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let metadata = AllocationMetadata::for_type(0xC003_520A)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let mut small_storage = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut large_storage = [0usize; TYPE_CACHE_NODE_WORDS * 2];
            let small =
                Layout::from_size_align(size_of_val(&small_storage), align_of::<usize>()).unwrap();
            let large =
                Layout::from_size_align(size_of_val(&large_storage), align_of::<usize>()).unwrap();
            let small_retained = type_cache_retained_bytes_for_layout(small);
            let large_retained = type_cache_retained_bytes_for_layout(large);
            let small_ptr = small_storage.as_mut_ptr() as *mut u8;
            let large_ptr = large_storage.as_mut_ptr() as *mut u8;
            let mut bucket = SegregatedTypeCacheBucket::empty();

            assert!(bucket
                .push(SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key,
                    ptr: small_ptr,
                    size: small.size(),
                    align: small.align(),
                    auth: 0,
                    metadata,
                })
                .is_none());
            assert_eq!(bucket.count, 1);
            assert_eq!(bucket.retained_bytes, small_retained);
            assert_eq!(bucket.retained_bytes(), Some(small_retained));

            assert!(bucket
                .push(SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key,
                    ptr: large_ptr,
                    size: large.size(),
                    align: large.align(),
                    auth: 0,
                    metadata,
                })
                .is_none());
            assert_eq!(bucket.count, 2);
            assert_eq!(bucket.retained_bytes, small_retained + large_retained);
            assert_eq!(
                bucket.retained_bytes(),
                Some(small_retained + large_retained)
            );

            let popped_large = bucket
                .pop(large, cache_key, policy_key)
                .expect("large entry should be reusable");
            assert_eq!(popped_large.ptr, large_ptr);
            assert_eq!(bucket.count, 1);
            assert_eq!(bucket.retained_bytes, small_retained);
            assert_eq!(bucket.retained_bytes(), Some(small_retained));

            let popped_small = bucket
                .pop(small, cache_key, policy_key)
                .expect("small entry should be reusable");
            assert_eq!(popped_small.ptr, small_ptr);
            assert_eq!(bucket.count, 0);
            assert_eq!(bucket.retained_bytes, 0);
            assert_eq!(bucket.retained_bytes(), Some(0));
        }
    }

    #[test]
    fn segregated_type_cache_retained_bytes_use_allocator_rounded_size() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let metadata = AllocationMetadata::for_type(0xC003_5212)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let cache_key = type_cache_identity_key(metadata);
            let policy_key = segregated_type_cache_policy_key(metadata);
            let mut storage = [0usize; TYPE_CACHE_NODE_WORDS + 4];
            let layout = first_unrounded_type_cache_layout_for_test();
            assert!(
                layout.size() <= size_of_val(&storage),
                "test storage must cover the requested unrounded layout"
            );
            let retained = type_cache_retained_bytes_for_layout(layout);
            assert!(
                retained > layout.size(),
                "test layout must exercise internal-fragmentation accounting"
            );
            let ptr = storage.as_mut_ptr() as *mut u8;
            let mut bucket = SegregatedTypeCacheBucket::empty();

            assert!(bucket
                .push(SegregatedTypeCacheEntry {
                    cache_key,
                    type_id: metadata.type_id,
                    policy_key,
                    ptr,
                    size: layout.size(),
                    align: layout.align(),
                    auth: 0,
                    metadata,
                })
                .is_none());
            assert_eq!(bucket.count, 1);
            assert_eq!(bucket.retained_bytes, retained);
            assert_eq!(bucket.retained_bytes(), Some(retained));
            assert!(bucket.can_accept_object_size(layout.size()));

            let popped = bucket
                .pop(layout, cache_key, policy_key)
                .expect("rounded-accounted entry should still match by exact layout");
            assert_eq!(popped.ptr, ptr);
            assert_eq!(bucket.count, 0);
            assert_eq!(bucket.retained_bytes, 0);
            assert_eq!(bucket.retained_bytes(), Some(0));
        }
    }

    #[test]
    fn segregated_type_cache_bucket_byte_budget_allows_replacement_but_rejects_growth() {
        const EIGHTH_BUCKET_WORDS: usize =
            (MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES / 8) / core::mem::size_of::<usize>();
        const QUARTER_BUCKET_WORDS: usize =
            (MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES / 4) / core::mem::size_of::<usize>();

        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            let metadata = AllocationMetadata::for_type(0xC003_5209)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
            let small_layout = Layout::from_size_align(
                EIGHTH_BUCKET_WORDS * size_of::<usize>(),
                align_of::<usize>(),
            )
            .unwrap();
            let large_layout = Layout::from_size_align(
                QUARTER_BUCKET_WORDS * size_of::<usize>(),
                align_of::<usize>(),
            )
            .unwrap();
            let mut storage = [[0usize; EIGHTH_BUCKET_WORDS]; SEGREGATED_TYPE_CACHE_DEPTH];
            let mut replacement = [0usize; EIGHTH_BUCKET_WORDS];
            let mut bucket = SegregatedTypeCacheBucket::empty();

            for item in storage.iter_mut() {
                let ptr = item.as_mut_ptr() as *mut u8;
                assert!(bucket
                    .push(SegregatedTypeCacheEntry {
                        cache_key: type_cache_identity_key(metadata),
                        type_id: metadata.type_id,
                        policy_key: segregated_type_cache_policy_key(metadata),
                        ptr,
                        size: small_layout.size(),
                        align: small_layout.align(),
                        auth: 0,
                        metadata,
                    })
                    .is_none());
            }

            assert_eq!(
                bucket.retained_bytes(),
                Some(MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES)
            );
            assert_eq!(
                bucket.retained_bytes,
                MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES
            );
            assert!(
                bucket.can_accept_object_size(small_layout.size()),
                "a same-size push can evict one full bucket entry without growing retained bytes"
            );
            let replacement_ptr = replacement.as_mut_ptr() as *mut u8;
            let evicted = bucket
                .push(SegregatedTypeCacheEntry {
                    cache_key: type_cache_identity_key(metadata),
                    type_id: metadata.type_id,
                    policy_key: segregated_type_cache_policy_key(metadata),
                    ptr: replacement_ptr,
                    size: small_layout.size(),
                    align: small_layout.align(),
                    auth: 0,
                    metadata,
                })
                .expect("same-size replacement should evict one entry from the full bucket");
            assert!(!evicted.ptr.is_null());
            assert_eq!(
                bucket.retained_bytes,
                MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES
            );
            assert_eq!(
                bucket.retained_bytes(),
                Some(MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES)
            );
            assert!(
                !bucket.can_accept_object_size(large_layout.size()),
                "a larger replacement would exceed the bucket byte budget"
            );
        }
    }

    #[test]
    fn segregated_type_cache_aggregate_byte_budget_rejects_cross_bucket_overflow() {
        const QUARTER_BUCKET_WORDS: usize =
            (MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES / 4) / core::mem::size_of::<usize>();

        let _guard = test_guard();
        let mut storage: Vec<Box<[usize; QUARTER_BUCKET_WORDS]>> = Vec::new();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();

            let layout = Layout::from_size_align(
                QUARTER_BUCKET_WORDS * core::mem::size_of::<usize>(),
                align_of::<usize>(),
            )
            .unwrap();
            let retained = type_cache_retained_bytes_for_layout(layout);
            assert!(retained > 0);
            assert!(retained <= MAX_SEGREGATED_TYPE_CACHE_BUCKET_BYTES);

            let mut next_type = 0xC003_5400u64;
            let mut attempts = 0usize;
            while metadata_segregation_side_cache_snapshot()
                .retained_bytes
                .saturating_add(retained)
                <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
            {
                assert!(
                    attempts < 16_384,
                    "test should find enough insertable segregated cache keys"
                );
                let metadata = AllocationMetadata::for_type(next_type)
                    .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
                next_type = next_type.wrapping_add(1);
                attempts += 1;
                if !INLINE_SEGREGATED_TYPE_CACHE_ENTRY.is_empty()
                    && !metadata_segregated_growth_bucket_available_for_test(layout, metadata)
                {
                    continue;
                }

                storage.push(Box::new([0usize; QUARTER_BUCKET_WORDS]));
                let ptr = storage.last_mut().unwrap().as_mut_ptr() as *mut u8;
                match push_segregated_type_cache(ptr, layout, metadata) {
                    Ok(evicted) => {
                        assert!(
                            evicted.is_none(),
                            "budget-fill phase should grow into empty buckets, not replace"
                        );
                    }
                    Err(()) => {
                        storage.pop();
                    }
                }
            }

            let before_overflow = metadata_segregation_side_cache_snapshot();
            assert!(
                before_overflow.retained_bytes <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES,
                "accepted entries must stay within the aggregate budget"
            );
            assert!(
                before_overflow.retained_bytes.saturating_add(retained)
                    > MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES,
                "one more side-cache object must overflow the aggregate budget"
            );

            let mut overflow_accepted = false;
            let mut overflow_attempts = 0usize;
            while overflow_attempts < 16_384 {
                let metadata = AllocationMetadata::for_type(next_type)
                    .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);
                next_type = next_type.wrapping_add(1);
                overflow_attempts += 1;
                if !metadata_segregated_growth_bucket_available_for_test(layout, metadata) {
                    continue;
                }

                storage.push(Box::new([0usize; QUARTER_BUCKET_WORDS]));
                let ptr = storage.last_mut().unwrap().as_mut_ptr() as *mut u8;
                let before = metadata_segregation_side_cache_snapshot().retained_bytes;
                match push_segregated_type_cache(ptr, layout, metadata) {
                    Ok(evicted) => {
                        assert!(
                            evicted.is_none(),
                            "overflow candidate should have had an empty growth bucket"
                        );
                        overflow_accepted = true;
                        break;
                    }
                    Err(()) => {
                        storage.pop();
                        assert_eq!(
                            metadata_segregation_side_cache_snapshot().retained_bytes,
                            before,
                            "a rejected aggregate-budget push must not mutate retained bytes"
                        );
                    }
                }
            }

            assert!(
                !overflow_accepted,
                "metadata-segregated cache retention must be capped across distinct buckets"
            );
            assert!(
                metadata_segregation_side_cache_snapshot().retained_bytes
                    <= MAX_SEGREGATED_TYPE_CACHE_RETAINED_BYTES
            );
        }
    }

    #[test]
    fn type_cache_rejects_pointer_without_usize_node_alignment() {
        let _guard = test_guard();
        unsafe {
            if TYPE_CACHE_NODE_ALIGN <= 4 {
                return;
            }
            clear_type_cache_for_test();
            let mut storage = [0u8; MIN_TYPE_CACHE_OBJECT_SIZE + 2 * TYPE_CACHE_NODE_ALIGN];
            let base = storage.as_mut_ptr() as usize;
            let aligned = (base + TYPE_CACHE_NODE_ALIGN - 1) & !(TYPE_CACHE_NODE_ALIGN - 1);
            let ptr = (aligned + TYPE_CACHE_NODE_ALIGN / 2) as *mut u8;
            let layout = Layout::from_size_align(MIN_TYPE_CACHE_OBJECT_SIZE, 4).unwrap();
            let metadata = AllocationMetadata::for_type(90);

            assert_eq!((ptr as usize) & (layout.align() - 1), 0);
            assert_ne!((ptr as usize) & (TYPE_CACHE_NODE_ALIGN - 1), 0);
            assert!(!push_type_cache(ptr, layout, metadata));
            assert_eq!(pop_type_cache(layout, metadata), None);
        }
    }

    #[test]
    fn delayed_free_defers_type_cache_reuse_until_eviction() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            let mut backing = [[0usize; TYPE_CACHE_NODE_WORDS]; DELAYED_FREE_SLOTS + 1];
            let layout =
                Layout::from_size_align(size_of_val(&backing[0]), align_of::<usize>()).unwrap();
            let metadata =
                AllocationMetadata::for_type(99).with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);

            for slot in backing.iter_mut().take(DELAYED_FREE_SLOTS) {
                assert!(enqueue_delayed_free(
                    &RustAllocator::new(),
                    slot.as_mut_ptr() as *mut u8,
                    layout,
                    metadata
                )
                .is_none());
            }
            assert_eq!(
                pop_semantic_type_cache(layout, metadata.with_flags(FLAG_TYPE_ISOLATED)),
                None
            );

            let evicted = enqueue_delayed_free(
                &RustAllocator::new(),
                backing[DELAYED_FREE_SLOTS].as_mut_ptr() as *mut u8,
                layout,
                metadata,
            )
            .expect("ring should evict the oldest delayed free");
            assert_eq!(evicted.ptr, backing[0].as_mut_ptr() as *mut u8);
            release_delayed_slot(&RustAllocator::new(), evicted);
            assert_eq!(
                pop_semantic_type_cache(layout, metadata.with_flags(FLAG_TYPE_ISOLATED)),
                Some(backing[0].as_mut_ptr() as *mut u8)
            );
        }
    }

    #[test]
    fn delayed_free_occupied_mask_tracks_slots_and_repairs_missing_bits() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();

            let mut first = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut second = [0usize; TYPE_CACHE_NODE_WORDS];
            let mut larger = [0usize; TYPE_CACHE_NODE_WORDS + 1];
            let small_layout =
                Layout::from_size_align(size_of_val(&first), align_of::<usize>()).unwrap();
            let larger_layout =
                Layout::from_size_align(size_of_val(&larger), align_of::<usize>()).unwrap();
            let metadata = AllocationMetadata::for_type(0xD17A_0CC1)
                .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);

            assert!(enqueue_delayed_free(
                &RustAllocator::new(),
                first.as_mut_ptr() as *mut u8,
                small_layout,
                metadata,
            )
            .is_none());
            assert!(enqueue_delayed_free(
                &RustAllocator::new(),
                second.as_mut_ptr() as *mut u8,
                small_layout,
                metadata,
            )
            .is_none());
            assert_eq!(
                DELAYED_FREE_OCCUPIED_MASK & 0b11,
                0b11,
                "the occupied mask should track normal enqueue slots"
            );

            let taken = delayed_free_take_slot(0);
            assert_eq!(taken.ptr, first.as_mut_ptr() as *mut u8);
            assert_eq!(
                DELAYED_FREE_OCCUPIED_MASK & 0b11,
                0b10,
                "taking a delayed-free slot must clear only that slot bit"
            );

            assert!(enqueue_delayed_free(
                &RustAllocator::new(),
                larger.as_mut_ptr() as *mut u8,
                larger_layout,
                metadata,
            )
            .is_none());
            assert_eq!(
                DELAYED_FREE_OCCUPIED_MASK & 0b110,
                0b110,
                "the next cursor slot should be marked occupied"
            );

            DELAYED_FREE_OCCUPIED_MASK = 0b10;
            assert_eq!(
                delayed_free_largest_occupied_index(),
                Some(2),
                "a partially missing occupied-mask bit should trigger a slow repair scan and still find the largest retained slot"
            );
            assert_eq!(
                DELAYED_FREE_OCCUPIED_MASK & 0b110,
                0b110,
                "slow repair should rebuild the occupied mask from real slots"
            );

            clear_delayed_free_for_test();
            clear_type_cache_for_test();
        }
    }

    #[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
    #[test]
    fn delayed_free_retained_byte_budget_evicts_old_large_real_objects() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        const OBJECTS: usize = 4;
        let alloc = RustAllocator::new();
        let layout =
            Layout::from_size_align(MAX_DELAYED_FREE_RETAINED_BYTES / 2, align_of::<usize>())
                .unwrap();
        let metadata = AllocationMetadata::for_type(0xD17A_0001)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);
        let mut ptrs = [core::ptr::null_mut(); OBJECTS];

        for ptr in ptrs.iter_mut() {
            *ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
            assert!(!ptr.is_null());
        }
        for &ptr in ptrs.iter() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, layout, metadata);
            }
            let snap = delayed_free_snapshot();
            assert_delayed_free_snapshot_accounting(snap);
            assert!(
                snap.retained_bytes <= MAX_DELAYED_FREE_RETAINED_BYTES,
                "delayed-free retained bytes must stay within the quarantine budget"
            );
        }

        let snap = delayed_free_snapshot();
        assert_delayed_free_snapshot_accounting(snap);
        assert_eq!(snap.occupied_slots, 2);
        assert_eq!(snap.retained_bytes, MAX_DELAYED_FREE_RETAINED_BYTES);

        let stats = semantic_stats_snapshot();
        assert_eq!(stats.delayed_free_enqueues, OBJECTS);
        assert_eq!(
            stats.delayed_free_flushes,
            OBJECTS - 2,
            "the third and fourth frees should evict older large delayed-free slots"
        );

        semantic_stats_recording_disable();
        unsafe {
            release_delayed_free_for_test(&alloc);
            clear_type_cache_for_test();
        }
    }

    #[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
    #[test]
    fn delayed_free_multi_evicts_old_slots_to_retain_larger_real_object() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        const OLD_OBJECTS: usize = 3;
        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(
            MAX_TYPE_CACHE_OBJECT_SIZE + core::mem::size_of::<usize>(),
            align_of::<usize>(),
        )
        .unwrap();
        let incoming_layout =
            Layout::from_size_align(MAX_DELAYED_FREE_RETAINED_BYTES / 2, align_of::<usize>())
                .unwrap();
        let metadata = AllocationMetadata::for_type(0xD17A_0003)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);
        let mut old_ptrs = [core::ptr::null_mut(); OLD_OBJECTS];

        for ptr in old_ptrs.iter_mut() {
            *ptr = unsafe { alloc.alloc_with_metadata(old_layout, metadata) };
            assert!(!ptr.is_null());
        }
        let incoming = unsafe { alloc.alloc_with_metadata(incoming_layout, metadata) };
        assert!(!incoming.is_null());

        for &ptr in old_ptrs.iter() {
            unsafe {
                alloc.dealloc_with_metadata(ptr, old_layout, metadata);
            }
        }
        let before = delayed_free_snapshot();
        assert_delayed_free_snapshot_accounting(before);
        assert_eq!(before.occupied_slots, OLD_OBJECTS);
        assert_eq!(
            before.retained_bytes,
            OLD_OBJECTS * delayed_free_retained_bytes_for_layout(old_layout)
        );

        unsafe {
            alloc.dealloc_with_metadata(incoming, incoming_layout, metadata);
        }

        let after = delayed_free_snapshot();
        assert_delayed_free_snapshot_accounting(after);
        assert_eq!(
            after.occupied_slots, 2,
            "two old delayed slots must be evicted so the larger incoming object can be retained"
        );
        assert_eq!(
            after.retained_bytes,
            delayed_free_retained_bytes_for_layout(old_layout)
                + delayed_free_retained_bytes_for_layout(incoming_layout)
        );
        assert!(
            after.retained_bytes <= MAX_DELAYED_FREE_RETAINED_BYTES,
            "multi-eviction must preserve the delayed-free retained-byte cap"
        );

        let mut retained_old = 0usize;
        let mut retained_incoming = false;
        unsafe {
            for slot in delayed_free_slots_snapshot_for_test().iter() {
                if old_ptrs.iter().any(|&ptr| ptr == slot.ptr) {
                    retained_old += 1;
                }
                if slot.ptr == incoming {
                    retained_incoming = true;
                }
            }
        }
        assert_eq!(
            retained_old, 1,
            "largest-first multi-eviction should release two old oversized objects"
        );
        assert!(
            retained_incoming,
            "the incoming object should be quarantined instead of bypassing after multi-eviction"
        );

        let stats = semantic_stats_snapshot();
        assert_eq!(stats.delayed_free_enqueues, OLD_OBJECTS + 1);
        assert_eq!(stats.delayed_free_flushes, 2);

        semantic_stats_recording_disable();
        unsafe {
            release_delayed_free_for_test(&alloc);
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn delayed_free_retained_bytes_use_allocator_rounded_size() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();

        let alloc = RustAllocator::new();
        let layout = first_unrounded_type_cache_layout_for_test();
        let retained = delayed_free_retained_bytes_for_layout(layout);
        assert!(
            retained > layout.size(),
            "test layout must exercise delayed-free internal-fragmentation accounting"
        );
        let metadata = AllocationMetadata::for_type(0xD17A_0004)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let snap = delayed_free_snapshot();
        assert_delayed_free_snapshot_accounting(snap);
        assert_eq!(snap.occupied_slots, 1);
        assert_eq!(
            snap.retained_bytes, retained,
            "delayed-free quarantine should budget the allocator-rounded retained block"
        );

        unsafe {
            release_delayed_free_for_test(&alloc);
            clear_type_cache_for_test();
        }
    }

    #[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
    #[test]
    fn delayed_free_oversized_real_object_bypasses_quarantine() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(
            MAX_DELAYED_FREE_RETAINED_BYTES + core::mem::size_of::<usize>(),
            align_of::<usize>(),
        )
        .unwrap();
        let metadata = AllocationMetadata::for_type(0xD17A_0002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_DELAYED_FREE);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let snap = delayed_free_snapshot();
        assert_delayed_free_snapshot_accounting(snap);
        assert_eq!(snap.occupied_slots, 0);
        assert_eq!(snap.retained_bytes, 0);
        let stats = semantic_stats_snapshot();
        assert_eq!(
            stats.delayed_free_enqueues, 0,
            "oversized delayed-free objects should not be counted as retained quarantine enqueues"
        );
        assert_eq!(stats.delayed_free_flushes, 1);

        semantic_stats_recording_disable();
        unsafe {
            clear_delayed_free_for_test();
            clear_type_cache_for_test();
        }
    }

    #[test]
    fn ffi_rejects_invalid_layouts_without_allocating() {
        let _guard = test_guard();
        unsafe {
            assert!(__unialloc_alloc_with_metadata(8, 3, 1, 0, FLAG_TYPE_ISOLATED, 0).is_null());
            assert!(!__unialloc_dealloc_with_metadata(
                core::ptr::null_mut(),
                8,
                3,
                1,
                0,
                FLAG_TYPE_ISOLATED,
                0
            ));
            assert!(__unialloc_realloc_with_metadata(
                core::ptr::null_mut(),
                8,
                3,
                16,
                1,
                0,
                FLAG_TYPE_ISOLATED,
                0
            )
            .is_null());
            assert!(__unialloc_realloc_with_split_metadata(
                core::ptr::null_mut(),
                8,
                3,
                16,
                1,
                0,
                FLAG_TYPE_ISOLATED,
                0,
                2,
                0,
                FLAG_TYPE_ISOLATED,
                1,
            )
            .is_null());
        }
    }

    #[test]
    fn ffi_dealloc_accepts_null_for_valid_layout_without_raw_free() {
        let _guard = test_guard();
        semantic_stats_reset();
        unsafe {
            assert!(__unialloc_dealloc_with_metadata(
                core::ptr::null_mut(),
                8,
                8,
                1,
                0,
                FLAG_TYPE_ISOLATED,
                0
            ));
        }
        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.fallback_deallocations, 0);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn ffi_realloc_rejects_null_nonzero_old_layout_without_allocating() {
        let _guard = test_guard();
        semantic_stats_reset();
        let ptr = unsafe {
            __unialloc_realloc_with_metadata(
                core::ptr::null_mut(),
                8,
                8,
                16,
                0xFEED_1001,
                0,
                FLAG_TYPE_ISOLATED,
                0xC411_1001,
            )
        };
        assert!(ptr.is_null());
        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.typed_allocations, 0);
        assert_eq!(snap.typed_deallocations, 0);
    }

    #[cfg(feature = "stats")]
    #[test]
    fn ffi_realloc_accepts_null_zero_old_layout_as_fresh_allocation() {
        let _guard = test_guard();
        semantic_stats_reset();
        let ptr = unsafe {
            __unialloc_realloc_with_metadata(
                core::ptr::null_mut(),
                0,
                8,
                16,
                0xFEED_1001,
                0,
                FLAG_TYPE_ISOLATED,
                0xC411_1001,
            )
        };
        assert!(!ptr.is_null());
        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 0);
        unsafe {
            assert!(__unialloc_dealloc_with_metadata(
                ptr,
                16,
                8,
                0xFEED_1001,
                0,
                FLAG_TYPE_ISOLATED,
                0xC411_1001,
            ));
        }
    }

    #[test]
    fn ffi_realloc_rejects_invalid_new_layout_without_allocating() {
        let _guard = test_guard();
        semantic_stats_reset();
        let ptr = unsafe {
            __unialloc_realloc_with_metadata(
                core::ptr::null_mut(),
                8,
                8,
                usize::MAX,
                0xFEED_1002,
                0,
                FLAG_TYPE_ISOLATED,
                0xC411_1002,
            )
        };
        assert!(ptr.is_null());
        assert_eq!(semantic_stats_snapshot().total_allocations, 0);
    }

    #[test]
    fn ffi_realloc_split_rejects_null_nonzero_old_layout_without_allocating() {
        let _guard = test_guard();
        semantic_stats_reset();
        let ptr = unsafe {
            __unialloc_realloc_with_split_metadata(
                core::ptr::null_mut(),
                8,
                8,
                16,
                0xFEED_1003,
                0,
                FLAG_TYPE_ISOLATED,
                0xC411_1003,
                0xFEED_1004,
                0,
                FLAG_TYPE_ISOLATED,
                0xC411_1004,
            )
        };
        assert!(ptr.is_null());
        assert_eq!(semantic_stats_snapshot().total_allocations, 0);
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_stats_do_not_count_failed_typed_allocation() {
        let _guard = test_guard();
        let _cleanup = SemanticStateCleanup;
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(size_of::<usize>(), align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xFA11_EDA1)
            .with_module(0xC0DE)
            .with_callsite(0xA110_FA11)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_MEMORY_TAGGING);
        let ptr = unsafe { alloc.alloc_raw(layout) };
        assert!(!ptr.is_null(), "test needs a real raw allocation");
        unsafe {
            record_memory_tagged_allocation(ptr, layout, metadata)
                .expect("pre-existing tag record should force duplicate-record failure");
        }

        let finished =
            unsafe { alloc.finish_semantic_allocation_with_recovery(ptr, layout, metadata, true) };
        assert!(
            finished.is_null(),
            "duplicate memory-tag metadata must fail-close semantic allocation finishing"
        );

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.typed_allocations, 0);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(lookup_auto_allocation_metadata(ptr, layout), None);
        unsafe {
            clear_memory_tagged_allocation(ptr, layout, metadata);
        }
        semantic_stats_recording_disable();
    }

    #[test]
    fn ffi_can_reset_stats_for_fresh_evaluation_interval() {
        let _guard = test_guard();
        SEMANTIC_STATS.record_alloc(AllocationMetadata::for_type(7), 64);
        assert_ne!(semantic_stats_snapshot().total_allocations, 0);
        __unialloc_semantic_stats_reset();
        assert_eq!(semantic_stats_snapshot().total_allocations, 0);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn scoped_metadata_routes_global_alloc_through_typed_path() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();
        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0x5150)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE);

        let ptr = with_semantic_metadata(metadata, || unsafe { alloc.alloc(layout) });
        assert!(!ptr.is_null());
        with_semantic_metadata(metadata, || unsafe {
            alloc.dealloc(ptr, layout);
        });

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(active_allocation_metadata(), None);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_snapshot_records_per_type_counts() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_A110)
            .with_module(0x5354_4154)
            .with_callsite(0xC002_C411)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }; 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        assert!(count >= 1);
        let row = rows
            .iter()
            .find(|row| row.type_id == metadata.type_id)
            .expect("per-type stats row");
        assert_eq!(row.module_id, metadata.module_id);
        assert_eq!(row.callsite, metadata.callsite);
        assert_eq!(row.allocations, 1);
        assert_eq!(row.allocated_bytes, layout.size());
        assert_eq!(row.deallocations, 1);
        assert_eq!(row.observed_alloc_size, layout.size());
        assert_eq!(row.observed_alloc_align, layout.align());
        assert_eq!(row.observed_dealloc_size, layout.size());
        assert_eq!(row.observed_dealloc_align, layout.align());
        assert!(row.policy_flags_seen & FLAG_TYPE_ISOLATED != 0);
        assert!(row.policy_flags_seen & FLAG_METADATA_SEGREGATED != 0);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_keep_same_type_distinct_callsites() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let site_a = AllocationMetadata::for_type(0xC002_D15A)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_C501)
            .with_flags(FLAG_TYPE_ISOLATED);
        let site_b = AllocationMetadata::for_type(site_a.type_id)
            .with_module(site_a.module_id)
            .with_callsite(0xA110_C502)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        record_stats_alloc(site_a, 16);
        record_stats_alloc(site_b, 24);

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }; 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let matching: [_; 2] = [
            rows.iter()
                .take(count)
                .find(|row| row.type_id == site_a.type_id && row.callsite == site_a.callsite)
                .copied()
                .expect("site A row"),
            rows.iter()
                .take(count)
                .find(|row| row.type_id == site_b.type_id && row.callsite == site_b.callsite)
                .copied()
                .expect("site B row"),
        ];

        assert_eq!(matching[0].allocations, 1);
        assert_eq!(matching[0].allocated_bytes, 16);
        assert_eq!(matching[1].allocations, 1);
        assert_eq!(matching[1].allocated_bytes, 24);
        assert!(matching[1].policy_flags_seen & FLAG_METADATA_SEGREGATED != 0);
        semantic_stats_recording_disable();
    }

    #[test]
    fn semantic_type_stats_hash_includes_module_and_callsite() {
        let base = AllocationMetadata::for_type(0xC002_5151)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5151)
            .with_flags(FLAG_TYPE_ISOLATED);
        let same_type_other_site = AllocationMetadata::for_type(base.type_id)
            .with_module(base.module_id)
            .with_callsite(0xA110_5152)
            .with_flags(FLAG_TYPE_ISOLATED);
        let same_type_other_module = AllocationMetadata::for_type(base.type_id)
            .with_module(0x5354_4155)
            .with_callsite(base.callsite)
            .with_flags(FLAG_TYPE_ISOLATED);

        let slots = [
            semantic_type_stats_slot(base),
            semantic_type_stats_slot(same_type_other_site),
            semantic_type_stats_slot(same_type_other_module),
        ];
        assert!(
            slots[0] != slots[1] || slots[0] != slots[2],
            "site-aware semantic stats should not always hash only by type_id"
        );
    }

    #[test]
    fn semantic_type_stats_are_sharded_without_reducing_row_capacity() {
        assert_eq!(
            SEMANTIC_TYPE_STATS_SHARD_COUNT * SEMANTIC_TYPE_STATS_SHARD_SLOTS,
            SEMANTIC_TYPE_STATS_SLOTS,
            "sharding must not reduce the exported per-type stats row budget"
        );

        let base = AllocationMetadata::for_type(0xC002_5A5A)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5A5A)
            .with_flags(FLAG_TYPE_ISOLATED);
        let base_shard = semantic_type_stats_shard(base);
        let mut other = None;
        let mut offset = 1u64;
        while offset < 512 {
            let candidate = AllocationMetadata::for_type(base.type_id + offset)
                .with_module(base.module_id)
                .with_callsite(base.callsite + offset)
                .with_flags(FLAG_TYPE_ISOLATED);
            if semantic_type_stats_shard(candidate) != base_shard {
                other = Some(candidate);
                break;
            }
            offset += 1;
        }
        let other = other.expect("test metadata should spread across stats shards");
        assert_ne!(
            semantic_type_stats_shard(base),
            semantic_type_stats_shard(other)
        );
        assert_ne!(
            semantic_type_stats_slot(base),
            semantic_type_stats_slot(other)
        );
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_active_shard_mask_tracks_rows_and_reset() {
        let _guard = test_guard();
        semantic_stats_reset();

        let metadata = AllocationMetadata::for_type(0xC002_5A70)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5A70)
            .with_flags(FLAG_TYPE_ISOLATED);
        let shard_idx = semantic_type_stats_shard(metadata);

        assert_eq!(
            SEMANTIC_TYPE_STATS_ACTIVE_SHARDS.load(Ordering::Acquire),
            0,
            "reset should clear the sparse semantic-stats shard mask"
        );
        record_stats_alloc(metadata, 16);
        assert!(
            semantic_type_stats_shard_active(
                SEMANTIC_TYPE_STATS_ACTIVE_SHARDS.load(Ordering::Acquire),
                shard_idx
            ),
            "first per-type row should publish only the row's shard bit"
        );

        let mut rows = [empty_type_stats_row(); 1];
        assert_eq!(semantic_type_stats_snapshot(&mut rows), 1);
        assert_eq!(rows[0].type_id, metadata.type_id);
        assert_eq!(rows[0].allocated_bytes, 16);

        semantic_type_stats_reset();
        assert_eq!(
            SEMANTIC_TYPE_STATS_ACTIVE_SHARDS.load(Ordering::Acquire),
            0,
            "semantic stats reset must also reset the lock-skipping mask"
        );
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_shard_overflow_preserves_total_row_capacity() {
        let _guard = test_guard();
        semantic_stats_reset();

        let base = AllocationMetadata::for_type(0xC002_5B00)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5B00)
            .with_flags(FLAG_TYPE_ISOLATED);
        let home_shard = semantic_type_stats_shard(base);
        let mut sites = [AllocationMetadata::unknown(); SEMANTIC_TYPE_STATS_SHARD_SLOTS + 1];
        let mut found = 0usize;
        let mut offset = 0u64;
        while found < sites.len() && offset < 4096 {
            let candidate = AllocationMetadata::for_type(base.type_id + offset)
                .with_module(base.module_id)
                .with_callsite(base.callsite + offset)
                .with_flags(FLAG_TYPE_ISOLATED);
            if semantic_type_stats_shard(candidate) == home_shard {
                sites[found] = candidate;
                found += 1;
            }
            offset += 1;
        }
        assert_eq!(
            found,
            sites.len(),
            "test setup must find one more row than a single stats shard can hold"
        );

        for (idx, site) in sites.iter().enumerate() {
            record_stats_alloc(*site, idx + 1);
        }

        let mut rows = [empty_type_stats_row(); SEMANTIC_TYPE_STATS_SHARD_SLOTS + 1];
        let count = semantic_type_stats_snapshot(&mut rows);
        assert_eq!(
            count,
            sites.len(),
            "home-shard overflow should continue into another shard instead of dropping the row"
        );
        let overflow_site = sites[SEMANTIC_TYPE_STATS_SHARD_SLOTS];
        let overflow_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == overflow_site.type_id)
            .expect("overflow row preserved");
        assert_eq!(
            overflow_row.allocated_bytes,
            SEMANTIC_TYPE_STATS_SHARD_SLOTS + 1
        );
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_full_table_reports_dropped_events_and_reset() {
        let _guard = test_guard();
        semantic_stats_reset();

        for index in 0..=SEMANTIC_TYPE_STATS_SLOTS {
            let identity = index as u64 + 1;
            let metadata = AllocationMetadata::for_type(0xC002_6000 + identity)
                .with_module(0x5354_4154)
                .with_callsite(0xA110_6000 + identity)
                .with_flags(FLAG_TYPE_ISOLATED);
            record_stats_alloc(metadata, 1);
        }

        let mut rows = [empty_type_stats_row(); SEMANTIC_TYPE_STATS_SLOTS];
        assert_eq!(
            semantic_type_stats_snapshot(&mut rows),
            SEMANTIC_TYPE_STATS_SLOTS,
            "the fixed table should retain its full advertised row capacity"
        );
        assert_eq!(
            semantic_stats_snapshot().semantic_type_stats_dropped_events,
            1,
            "the first event beyond capacity must be observable instead of silently disappearing"
        );

        semantic_type_stats_reset();
        assert_eq!(
            semantic_stats_snapshot().semantic_type_stats_dropped_events,
            0,
            "reset must start a fresh completeness accounting interval"
        );
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_zero_sized_alloc_with_metadata_does_not_record_stats_or_recovery() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(0, 256).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_5155)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_5155)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe {
            with_auto_allocation_recovery_recording(|| alloc.alloc_with_metadata(layout, metadata))
        };
        assert_eq!(ptr as usize, layout.align());
        assert!(
            recorded_reallocation_old_metadata(ptr, layout).is_none(),
            "zero-sized semantic allocations must not create recovery records"
        );
        unsafe {
            alloc.dealloc_with_metadata(ptr, layout, metadata);
        }

        let ffi_align = 512;
        let ffi_ptr = unsafe {
            __unialloc_alloc_with_metadata(
                0,
                ffi_align,
                metadata.type_id + 1,
                metadata.module_id,
                FLAG_TYPE_ISOLATED,
                metadata.callsite + 1,
            )
        };
        assert_eq!(ffi_ptr as usize, ffi_align);
        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                ffi_ptr,
                0,
                ffi_align,
                metadata.type_id + 1,
                metadata.module_id,
                FLAG_TYPE_ISOLATED,
                metadata.callsite + 1,
            )
        });

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.typed_allocations, 0);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.total_allocated_bytes, 0);
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.fallback_deallocations, 0);

        let mut rows = [empty_type_stats_row(); 4];
        assert_eq!(semantic_type_stats_snapshot(&mut rows), 0);
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_realloc_split_metadata_records_old_dealloc_and_new_alloc_sites() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let new_size = 192;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC002_5101)
            .with_module(0x5350_4C54)
            .with_callsite(0xA110_5101)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC002_5102)
            .with_module(old_metadata.module_id)
            .with_callsite(0xA110_5102)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let old_ptr = unsafe { alloc.alloc_with_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());
        unsafe {
            core::ptr::write_bytes(old_ptr, 0xA5, old_layout.size());
        }

        let new_ptr = unsafe {
            alloc.realloc_with_split_metadata(
                old_ptr,
                old_layout,
                new_size,
                old_metadata,
                new_metadata,
            )
        };
        assert!(!new_ptr.is_null());

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(snap.last_type_id, new_metadata.type_id);

        let mut rows = [empty_type_stats_row(); 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let old_row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == old_metadata.type_id && row.callsite == old_metadata.callsite
            })
            .copied()
            .expect("old realloc site row");
        let new_row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == new_metadata.type_id && row.callsite == new_metadata.callsite
            })
            .copied()
            .expect("new realloc site row");
        assert_eq!(old_row.allocations, 1);
        assert_eq!(old_row.deallocations, 1);
        assert_eq!(new_row.allocations, 1);
        assert_eq!(new_row.deallocations, 0);
        assert!(new_row.policy_flags_seen & FLAG_METADATA_SEGREGATED != 0);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, old_layout, old_metadata);
            alloc.dealloc_raw(new_ptr, new_layout);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_realloc_split_metadata_to_zero_records_only_old_dealloc() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(96, align_of::<usize>()).unwrap();
        let old_metadata = AllocationMetadata::for_type(0xC002_5103)
            .with_module(0x5350_4C54)
            .with_callsite(0xA110_5103)
            .with_flags(FLAG_TYPE_ISOLATED);
        let new_metadata = AllocationMetadata::for_type(0xC002_5104)
            .with_module(old_metadata.module_id)
            .with_callsite(0xA110_5104)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let old_ptr = unsafe { alloc.alloc_with_metadata(old_layout, old_metadata) };
        assert!(!old_ptr.is_null());

        let zero_ptr = unsafe {
            alloc.realloc_with_split_metadata(old_ptr, old_layout, 0, old_metadata, new_metadata)
        };
        assert_eq!(zero_ptr as usize, old_layout.align());

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.total_allocated_bytes, old_layout.size());
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(snap.last_type_id, old_metadata.type_id);

        let mut rows = [empty_type_stats_row(); 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let old_row = rows
            .iter()
            .take(count)
            .find(|row| {
                row.type_id == old_metadata.type_id && row.callsite == old_metadata.callsite
            })
            .copied()
            .expect("old realloc-to-zero site row");
        assert_eq!(old_row.allocations, 1);
        assert_eq!(old_row.deallocations, 1);
        assert!(
            rows.iter()
                .take(count)
                .all(|row| row.type_id != new_metadata.type_id),
            "realloc-to-zero must not invent a new allocation-site row"
        );

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, old_layout, old_metadata);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn ffi_realloc_split_metadata_preserves_distinct_old_and_new_sites() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let old_layout = Layout::from_size_align(128, align_of::<usize>()).unwrap();
        let new_size = 256;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let module_id = 0x5350_4C54;
        let old_type_id = 0xC002_5201;
        let new_type_id = 0xC002_5202;
        let old_callsite = 0xA110_5201;
        let new_callsite = 0xA110_5202;

        let old_ptr = unsafe {
            __unialloc_alloc_with_metadata(
                old_layout.size(),
                old_layout.align(),
                old_type_id,
                module_id,
                FLAG_TYPE_ISOLATED,
                old_callsite,
            )
        };
        assert!(!old_ptr.is_null());
        let new_ptr = unsafe {
            __unialloc_realloc_with_split_metadata(
                old_ptr,
                old_layout.size(),
                old_layout.align(),
                new_size,
                old_type_id,
                module_id,
                FLAG_TYPE_ISOLATED,
                old_callsite,
                new_type_id,
                module_id,
                FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED,
                new_callsite,
            )
        };
        assert!(!new_ptr.is_null());

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.last_type_id, new_type_id);

        let mut rows = [empty_type_stats_row(); 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let old_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == old_type_id && row.callsite == old_callsite)
            .copied()
            .expect("old FFI realloc site row");
        let new_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == new_type_id && row.callsite == new_callsite)
            .copied()
            .expect("new FFI realloc site row");
        assert_eq!(old_row.allocations, 1);
        assert_eq!(old_row.deallocations, 1);
        assert_eq!(new_row.allocations, 1);
        assert_eq!(new_row.deallocations, 0);

        semantic_stats_recording_disable();
        let alloc = RustAllocator::new();
        unsafe {
            drain_semantic_cache_for_test(
                &alloc,
                old_layout,
                AllocationMetadata::for_type(old_type_id)
                    .with_module(module_id)
                    .with_callsite(old_callsite)
                    .with_flags(FLAG_TYPE_ISOLATED),
            );
            alloc.dealloc_raw(new_ptr, new_layout);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn semantic_realloc_same_size_class_reuses_pointer_and_initializes_growth() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires a same-size-class growth size");
        let layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_5301)
            .with_module(0x5352_4541)
            .with_callsite(0xA110_5301)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
        assert!(!ptr.is_null());
        unsafe {
            core::ptr::write_bytes(ptr, 0xA5, old_size);
        }

        let grown = unsafe { alloc.realloc_with_metadata(ptr, layout, new_size, metadata) };
        assert_eq!(grown, ptr);
        for offset in old_size..new_size {
            assert_eq!(unsafe { *grown.add(offset) }, 0);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.typed_cache_inserts, 0);
        assert_eq!(snap.last_type_id, metadata.type_id);

        semantic_stats_recording_disable();
        unsafe {
            alloc.dealloc_raw(
                grown,
                Layout::from_size_align(new_size, layout.align()).unwrap(),
            );
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn semantic_realloc_split_same_storage_reuses_pointer_with_new_callsite() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires a same-size-class growth size");
        let layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let module_id = 0x5352_4541;
        let type_id = 0xC002_5302;
        let old_metadata = AllocationMetadata::for_type(type_id)
            .with_module(module_id)
            .with_callsite(0xA110_5302)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE);
        let new_metadata = AllocationMetadata::for_type(type_id)
            .with_module(module_id)
            .with_callsite(0xA110_5303)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE);

        let ptr = unsafe { alloc.alloc_with_metadata(layout, old_metadata) };
        assert!(!ptr.is_null());
        unsafe {
            core::ptr::write_bytes(ptr, 0xA5, old_size);
        }

        let grown = unsafe {
            alloc.realloc_with_split_metadata(ptr, layout, new_size, old_metadata, new_metadata)
        };
        assert_eq!(grown, ptr);
        for offset in old_size..new_size {
            assert_eq!(unsafe { *grown.add(offset) }, 0);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.typed_cache_inserts, 0);
        assert_eq!(snap.last_type_id, type_id);

        let mut rows = [empty_type_stats_row(); 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let old_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == type_id && row.callsite == old_metadata.callsite)
            .copied()
            .expect("old callsite row");
        let new_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == type_id && row.callsite == new_metadata.callsite)
            .copied()
            .expect("new callsite row");
        assert_eq!(old_row.allocations, 1);
        assert_eq!(old_row.deallocations, 1);
        assert_eq!(new_row.allocations, 1);
        assert_eq!(new_row.deallocations, 0);

        semantic_stats_recording_disable();
        unsafe {
            alloc.dealloc_raw(
                grown,
                Layout::from_size_align(new_size, layout.align()).unwrap(),
            );
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn ffi_realloc_recovers_old_allocation_metadata_for_split_callsite_stats() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires same-size-class growth");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let module_id = 0x5245_4646;
        let type_id = 0xC002_5403;
        let old_metadata = AllocationMetadata::for_type(type_id)
            .with_module(module_id)
            .with_callsite(0xA110_5403)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE);
        let new_metadata = AllocationMetadata::for_type(type_id)
            .with_module(module_id)
            .with_callsite(0xA110_5404)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                old_layout.size(),
                old_layout.align(),
                old_metadata.type_id,
                old_metadata.module_id,
                old_metadata.flags,
                old_metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            Some(old_metadata)
        );
        unsafe {
            core::ptr::write_bytes(ptr, 0xA5, old_size);
        }

        let grown = unsafe {
            __unialloc_realloc_with_metadata(
                ptr,
                old_layout.size(),
                old_layout.align(),
                new_size,
                new_metadata.type_id,
                new_metadata.module_id,
                new_metadata.flags,
                new_metadata.callsite,
            )
        };
        assert_eq!(grown, ptr);
        for offset in old_size..new_size {
            assert_eq!(unsafe { *grown.add(offset) }, 0);
        }
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            None,
            "realloc must consume the allocation-site recovery metadata before installing the new site"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(grown, new_layout),
            Some(new_metadata)
        );

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.last_type_id, type_id);

        let mut rows = [empty_type_stats_row(); 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let old_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == type_id && row.callsite == old_metadata.callsite)
            .copied()
            .expect("old FFI allocation callsite row");
        let new_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == type_id && row.callsite == new_metadata.callsite)
            .copied()
            .expect("new FFI realloc callsite row");
        assert_eq!(old_row.allocations, 1);
        assert_eq!(old_row.deallocations, 1);
        assert_eq!(new_row.allocations, 1);
        assert_eq!(new_row.deallocations, 0);

        semantic_stats_recording_disable();
        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                grown,
                new_layout.size(),
                new_layout.align(),
                new_metadata.type_id,
                new_metadata.module_id,
                new_metadata.flags,
                new_metadata.callsite,
            )
        });
        unsafe {
            if let Some(cached) = pop_semantic_type_cache(new_layout, new_metadata) {
                RustAllocator::new().dealloc_raw(cached, new_layout);
            }
            clear_auto_allocation_records();
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn ffi_realloc_in_place_keeps_fast_recovery_record_when_stats_enable() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        semantic_type_stats_recording_disable();

        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires same-size-class growth");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_5401)
            .with_module(0x5245_414C)
            .with_callsite(0xA110_5401)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                old_layout.size(),
                old_layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            Some(metadata)
        );
        assert!(current_thread_fast_auto_allocation_records_active());
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        semantic_stats_recording_enable();
        let grown = unsafe {
            __unialloc_realloc_with_metadata(
                ptr,
                old_layout.size(),
                old_layout.align(),
                new_size,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert_eq!(grown, ptr);
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            None,
            "in-place realloc must consume the old TLS recovery record"
        );
        assert!(
            current_thread_fast_auto_allocation_records_active(),
            "new stats-visible same-thread record should stay in the TLS fast table"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, new_layout),
            Some(metadata)
        );
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "stats-enabled same-thread recovery metadata should not require the global mutex table"
        );

        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                grown,
                new_layout.size(),
                new_layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        });
        assert_eq!(lookup_auto_allocation_metadata(ptr, new_layout), None);
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);

        unsafe {
            if let Some(cached) = pop_semantic_type_cache(new_layout, metadata) {
                RustAllocator::new().dealloc_raw(cached, new_layout);
            }
            clear_auto_allocation_records();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn ffi_realloc_in_place_keeps_fast_recovery_record_when_stats_disable() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
            clear_auto_allocation_records();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires same-size-class growth");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let metadata = AllocationMetadata::for_type(0xC002_5402)
            .with_module(0x5245_414C)
            .with_callsite(0xA110_5402)
            .with_flags(FLAG_TYPE_ISOLATED);

        let ptr = unsafe {
            __unialloc_alloc_with_metadata(
                old_layout.size(),
                old_layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert!(!ptr.is_null());
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            Some(metadata)
        );
        assert_eq!(AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed), 0);
        assert!(current_thread_fast_auto_allocation_records_active());

        semantic_stats_recording_disable();
        let grown = unsafe {
            __unialloc_realloc_with_metadata(
                ptr,
                old_layout.size(),
                old_layout.align(),
                new_size,
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        };
        assert_eq!(grown, ptr);
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, old_layout),
            None,
            "in-place realloc must consume the old global recovery record"
        );
        assert_eq!(
            AUTO_ALLOCATION_RECORD_COUNT.load(Ordering::Relaxed),
            0,
            "new same-thread recovery record should not leave stale global state"
        );
        assert_eq!(
            lookup_auto_allocation_metadata(ptr, new_layout),
            Some(metadata)
        );
        assert!(current_thread_fast_auto_allocation_records_active());

        assert!(unsafe {
            __unialloc_dealloc_with_metadata(
                grown,
                new_layout.size(),
                new_layout.align(),
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            )
        });
        assert_eq!(lookup_auto_allocation_metadata(ptr, new_layout), None);
        assert!(!current_thread_fast_auto_allocation_records_active());

        unsafe {
            if let Some(cached) = pop_semantic_type_cache(new_layout, metadata) {
                RustAllocator::new().dealloc_raw(cached, new_layout);
            }
            clear_auto_allocation_records();
        }
        semantic_stats_recording_disable();
    }

    #[cfg(feature = "stats")]
    #[test]
    fn semantic_type_stats_snapshot_includes_other_threads() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let main_site = AllocationMetadata::for_type(0xC002_7001)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_7001)
            .with_flags(FLAG_TYPE_ISOLATED);
        let worker_site = AllocationMetadata::for_type(0xC002_7002)
            .with_module(0x5354_4154)
            .with_callsite(0xA110_7002)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

        let worker = thread::spawn(move || {
            record_stats_alloc(worker_site, 64);
            record_stats_dealloc(worker_site);
        });
        record_stats_alloc(main_site, 32);
        record_stats_dealloc(main_site);
        worker.join().expect("worker semantic stats");

        let mut rows = [SemanticTypeStatsSnapshot {
            type_id: UNKNOWN_SEMANTIC_ID,
            module_id: UNKNOWN_SEMANTIC_ID,
            callsite: 0,
            allocations: 0,
            allocated_bytes: 0,
            deallocations: 0,
            cache_hits: 0,
            cache_inserts: 0,
            cache_bypasses: 0,
            observed_alloc_size: 0,
            observed_alloc_align: 0,
            observed_dealloc_size: 0,
            observed_dealloc_align: 0,
            policy_flags_seen: 0,
        }; 8];
        let count = semantic_type_stats_snapshot(&mut rows);
        let main_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == main_site.type_id && row.callsite == main_site.callsite)
            .expect("main thread row");
        let worker_row = rows
            .iter()
            .take(count)
            .find(|row| row.type_id == worker_site.type_id && row.callsite == worker_site.callsite)
            .expect("worker thread row");

        assert_eq!(main_row.allocations, 1);
        assert_eq!(main_row.allocated_bytes, 32);
        assert_eq!(main_row.deallocations, 1);
        assert_eq!(worker_row.allocations, 1);
        assert_eq!(worker_row.allocated_bytes, 64);
        assert_eq!(worker_row.deallocations, 1);
        assert!(worker_row.policy_flags_seen & FLAG_METADATA_SEGREGATED != 0);
        semantic_stats_recording_disable();
    }
    #[cfg(feature = "stats")]
    #[test]
    fn auto_metadata_routes_unscoped_global_alloc_through_typed_path() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_enable(AUTO_LAYOUT_MODULE_ID, FLAG_TYPE_ISOLATED, 0xA11C);

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(72, align_of::<usize>()).unwrap();
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc(ptr, layout);
        }
        semantic_auto_metadata_disable();

        let snap = semantic_stats_snapshot();
        assert!(snap.total_allocations >= 1);
        assert!(snap.typed_allocations >= 1);
        assert_ne!(snap.last_type_id, UNKNOWN_SEMANTIC_ID);
        assert!(snap.policy_flags_seen & FLAG_TYPE_ISOLATED != 0);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn fallback_global_alloc_stats_are_opt_in() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();
        assert!(!semantic_runtime_slow_path_enabled());

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc(ptr, layout);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.fallback_deallocations, 0);

        semantic_stats_reset();
        assert!(semantic_stats_recording_enabled());
        assert!(semantic_runtime_slow_path_enabled());
        let ptr = unsafe { alloc.alloc(layout) };
        assert!(!ptr.is_null());
        unsafe {
            alloc.dealloc(ptr, layout);
        }
        semantic_stats_recording_disable();

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.fallback_allocations, 1);
        assert_eq!(snap.fallback_deallocations, 1);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn fallback_global_realloc_stats_record_moved_old_block() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let new_size = 4096;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let old_ptr = unsafe { alloc.alloc(old_layout) };
        assert!(!old_ptr.is_null());

        semantic_stats_reset();
        let new_ptr = unsafe { alloc.realloc(old_ptr, old_layout, new_size) };
        assert!(!new_ptr.is_null());
        assert_ne!(new_ptr, old_ptr);
        semantic_stats_recording_disable();

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.fallback_allocations, 1);
        assert_eq!(snap.fallback_deallocations, 1);

        unsafe {
            alloc.dealloc_raw(new_ptr, new_layout);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn fallback_global_realloc_from_zero_records_alloc_not_dealloc() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(0, 256).unwrap();
        let new_size = 64;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();
        let old_ptr = unsafe { alloc.alloc(old_layout) };
        assert_eq!(old_ptr as usize, old_layout.align());

        semantic_stats_reset();
        let new_ptr = unsafe { alloc.realloc(old_ptr, old_layout, new_size) };
        assert!(!new_ptr.is_null());
        assert_ne!(new_ptr, old_ptr);
        semantic_stats_recording_disable();

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.fallback_allocations, 1);
        assert_eq!(snap.total_allocated_bytes, new_size);
        assert_eq!(snap.fallback_allocated_bytes, new_size);
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.fallback_deallocations, 0);

        unsafe {
            alloc.dealloc_raw(new_ptr, new_layout);
        }
    }

    #[cfg(feature = "stats")]
    #[test]
    fn fallback_global_realloc_to_zero_records_dealloc_not_alloc() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();
        let old_ptr = unsafe { alloc.alloc(old_layout) };
        assert!(!old_ptr.is_null());

        semantic_stats_reset();
        let zero_ptr = unsafe { alloc.realloc(old_ptr, old_layout, 0) };
        assert_eq!(zero_ptr as usize, old_layout.align());
        semantic_stats_recording_disable();

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.fallback_allocations, 0);
        assert_eq!(snap.total_allocated_bytes, 0);
        assert_eq!(snap.fallback_allocated_bytes, 0);
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.fallback_deallocations, 1);
    }

    #[cfg(feature = "stats")]
    #[test]
    fn typed_semantic_stats_are_opt_in() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        SEMANTIC_STATS.reset();
        semantic_stats_recording_disable();

        let layout = Layout::from_size_align(
            TYPE_CACHE_NODE_WORDS * size_of::<usize>(),
            align_of::<usize>(),
        )
        .unwrap();
        let metadata = AllocationMetadata::for_type(0x5151).with_flags(FLAG_TYPE_ISOLATED);
        let mut backing = [0usize; TYPE_CACHE_NODE_WORDS];
        let ptr = backing.as_mut_ptr() as *mut u8;
        unsafe {
            record_stats_alloc_layout(metadata, layout);
            record_stats_dealloc(metadata);
            assert!(push_type_cache(ptr, layout, metadata));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
            assert!(enqueue_delayed_free(
                &RustAllocator::new(),
                ptr,
                layout,
                metadata.with_flags(FLAG_DELAYED_FREE)
            )
            .is_none());
            clear_delayed_free_for_test();
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 0);
        assert_eq!(snap.typed_allocations, 0);
        assert_eq!(snap.typed_deallocations, 0);
        assert_eq!(snap.typed_cache_hits, 0);
        assert_eq!(snap.typed_cache_inserts, 0);
        assert_eq!(snap.typed_cache_bypasses, 0);
        assert_eq!(snap.delayed_free_enqueues, 0);

        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        assert!(semantic_stats_recording_enabled());
        let mut backing = [0usize; TYPE_CACHE_NODE_WORDS];
        let ptr = backing.as_mut_ptr() as *mut u8;
        unsafe {
            record_stats_alloc_layout(metadata, layout);
            record_stats_dealloc(metadata);
            assert!(push_type_cache(ptr, layout, metadata));
            assert_eq!(pop_type_cache(layout, metadata), Some(ptr));
            assert!(enqueue_delayed_free(
                &RustAllocator::new(),
                ptr,
                layout,
                metadata.with_flags(FLAG_DELAYED_FREE)
            )
            .is_none());
            clear_delayed_free_for_test();
        }
        semantic_stats_recording_disable();

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 1);
        assert_eq!(snap.typed_allocations, 1);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.typed_cache_inserts, 1);
        assert_eq!(snap.typed_cache_hits, 1);
        assert_eq!(snap.delayed_free_enqueues, 1);
    }
    #[cfg(feature = "stats")]
    #[test]
    fn global_alloc_dealloc_does_not_advance_compiler_site_stream() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let ids = [0xC002_3001_u64, 0xC002_3002_u64];
        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C300,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(72, align_of::<usize>()).unwrap();
        let first = unsafe { alloc.alloc(layout) };
        assert!(!first.is_null());
        unsafe {
            alloc.dealloc(first, layout);
        }
        let second = unsafe { alloc.alloc(layout) };
        assert!(!second.is_null());
        unsafe {
            alloc.dealloc(second, layout);
        }

        assert_eq!(auto_allocation_metadata(layout), None);
        semantic_auto_metadata_disable();

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 2);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(snap.typed_cache_inserts, 2);
        assert_eq!(snap.last_type_id, ids[1]);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(
                &alloc,
                layout,
                AllocationMetadata::for_type(ids[0]).with_flags(FLAG_TYPE_ISOLATED),
            );
            drain_semantic_cache_for_test(
                &alloc,
                layout,
                AllocationMetadata::for_type(ids[1]).with_flags(FLAG_TYPE_ISOLATED),
            );
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn global_alloc_realloc_recovers_compiler_stream_dealloc_metadata() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let ids = [0xC002_4001_u64, 0xC002_4002_u64];
        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED,
                0xA110_C400,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let old_layout = Layout::from_size_align(72, align_of::<usize>()).unwrap();
        let new_size = 144;
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();

        let old_ptr = unsafe { alloc.alloc(old_layout) };
        assert!(!old_ptr.is_null());
        let new_ptr = unsafe { alloc.realloc(old_ptr, old_layout, new_size) };
        assert!(!new_ptr.is_null());

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(snap.typed_cache_inserts, 1);
        assert_eq!(snap.last_type_id, ids[1]);
        assert_eq!(auto_allocation_metadata(new_layout), None);

        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(
                &alloc,
                old_layout,
                AllocationMetadata::for_type(ids[0]).with_flags(FLAG_TYPE_ISOLATED),
            );
            alloc.dealloc_raw(new_ptr, new_layout);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn global_alloc_realloc_compiler_stream_same_storage_reuses_pointer() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        let type_id = 0xC002_4003_u64;
        let ids = [type_id, type_id];
        assert!(unsafe {
            semantic_auto_compiler_metadata_stream_enable(
                0xC0DE,
                FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
                0xA110_C430,
                ids.as_ptr(),
                ids.len(),
            )
        });

        let alloc = RustAllocator::new();
        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires a same-size-class growth size");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();

        let old_ptr = unsafe { alloc.alloc(old_layout) };
        assert!(!old_ptr.is_null());
        unsafe {
            core::ptr::write_bytes(old_ptr, 0xA5, old_size);
        }
        let grown = unsafe { alloc.realloc(old_ptr, old_layout, new_size) };
        assert_eq!(grown, old_ptr);
        for offset in old_size..new_size {
            assert_eq!(unsafe { *grown.add(offset) }, 0);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(snap.typed_cache_inserts, 0);
        assert_eq!(snap.last_type_id, type_id);
        assert_eq!(auto_allocation_metadata(new_layout), None);

        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            alloc.dealloc_raw(grown, new_layout);
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn global_alloc_realloc_layout_auto_same_class_reuses_pointer() {
        let _guard = test_guard();
        unsafe {
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_stats_reset();
        semantic_auto_metadata_disable();
        semantic_auto_metadata_enable(
            AUTO_LAYOUT_MODULE_ID,
            FLAG_TYPE_ISOLATED | FLAG_FORCE_INITIALIZE,
            0xA11C_5001,
        );

        let alloc = RustAllocator::new();
        let old_size = 57;
        let old_class = crate::size_class::get_size_class(old_size).index();
        let new_size = (old_size + 1..old_size + 256)
            .find(|size| crate::size_class::get_size_class(*size).index() == old_class)
            .expect("test requires a same-size-class growth size");
        let old_layout = Layout::from_size_align(old_size, align_of::<usize>()).unwrap();
        let new_layout = Layout::from_size_align(new_size, old_layout.align()).unwrap();

        let old_ptr = unsafe { alloc.alloc(old_layout) };
        assert!(!old_ptr.is_null());
        unsafe {
            core::ptr::write_bytes(old_ptr, 0xA5, old_size);
        }
        let grown = unsafe { alloc.realloc(old_ptr, old_layout, new_size) };
        assert_eq!(grown, old_ptr);
        for offset in old_size..new_size {
            assert_eq!(unsafe { *grown.add(offset) }, 0);
        }

        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 1);
        assert_eq!(snap.fallback_deallocations, 0);
        assert_eq!(snap.typed_cache_inserts, 0);
        assert_eq!(
            snap.last_type_id,
            semantic_layout_id(new_size, new_layout.align())
        );

        semantic_auto_metadata_disable();
        semantic_stats_recording_disable();
        unsafe {
            alloc.dealloc_raw(grown, new_layout);
        }
    }

    #[test]
    fn ffi_scope_enter_exit_restores_previous_metadata() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        let outer = AllocationMetadata::for_type(1).with_flags(FLAG_TYPE_ISOLATED);
        let inner = AllocationMetadata::for_type(2).with_flags(FLAG_METADATA_SEGREGATED);

        let prev_outer = __unialloc_semantic_scope_enter(
            outer.type_id,
            outer.module_id,
            outer.flags,
            outer.callsite,
        );
        assert_eq!(prev_outer, AllocationMetadata::unknown());
        assert_eq!(active_allocation_metadata().unwrap().type_id, outer.type_id);

        let prev_inner = __unialloc_semantic_scope_enter(
            inner.type_id,
            inner.module_id,
            inner.flags,
            inner.callsite,
        );
        assert_eq!(prev_inner.type_id, outer.type_id);
        assert_eq!(active_allocation_metadata().unwrap().type_id, inner.type_id);

        __unialloc_semantic_scope_exit(prev_inner);
        assert_eq!(active_allocation_metadata().unwrap().type_id, outer.type_id);
        __unialloc_semantic_scope_exit(prev_outer);
        assert_eq!(active_allocation_metadata(), None);
    }

    #[test]
    fn ffi_scope_push_pop_restores_manual_outer_metadata() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        let outer = AllocationMetadata::for_type(0xC002_5101)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_callsite(0xA110_5101);
        let inner = AllocationMetadata::for_type(0xC002_5102)
            .with_module(0xC0DE)
            .with_flags(FLAG_METADATA_SEGREGATED)
            .with_callsite(0xA110_5102);

        let previous = unsafe { set_active_metadata(outer) };
        assert_eq!(previous, AllocationMetadata::unknown());
        __unialloc_semantic_scope_push(inner.type_id, inner.module_id, inner.flags, inner.callsite);
        assert_eq!(active_allocation_metadata(), Some(inner));
        __unialloc_semantic_scope_pop();
        assert_eq!(
            active_allocation_metadata(),
            Some(outer),
            "optimized base push/pop must still restore a non-default outer scope"
        );

        unsafe {
            reset_semantic_scope_stack_for_test();
        }
    }

    #[test]
    fn ffi_scope_push_hints_sets_full_active_metadata() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        let metadata = AllocationMetadata::for_type(0xC002_5104)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x51)
            .with_placement_hint(0x52)
            .with_callsite(0xA110_5104);

        __unialloc_semantic_scope_push_hints(
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.lifetime_hint,
            metadata.placement_hint,
            metadata.callsite,
        );
        assert_eq!(
            active_allocation_metadata(),
            Some(metadata),
            "MIR scope hint ABI must preserve full metadata, not silently zero policy hints"
        );
        __unialloc_semantic_scope_pop();
        assert_eq!(active_allocation_metadata(), None);

        unsafe {
            reset_semantic_scope_stack_for_test();
        }
    }

    #[test]
    fn ffi_scope_push_local_marks_metadata_as_no_recovery() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        let metadata = AllocationMetadata::for_type(0xC002_5105)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_callsite(0xA110_5105);

        __unialloc_semantic_scope_push_local(
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.callsite,
        );
        let active = active_allocation_metadata().expect("local scope metadata active");
        assert_eq!(active.type_id, metadata.type_id);
        assert_eq!(active.module_id, metadata.module_id);
        assert_eq!(active.flags, metadata.flags);
        assert_eq!(active.callsite, metadata.callsite);
        assert_eq!(
            active.placement_hint & PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY,
            PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY
        );
        assert!(
            !active_allocation_metadata_requires_recovery_record(active),
            "local compiler scope should not force allocation recovery bookkeeping"
        );

        __unialloc_semantic_scope_pop();
        assert_eq!(active_allocation_metadata(), None);
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
    }

    #[test]
    fn ffi_scope_push_hints_local_preserves_hints_and_marks_no_recovery() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        let metadata = AllocationMetadata::for_type(0xC002_5106)
            .with_module(0xC0DE)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_PROTECTION)
            .with_lifetime_hint(0x61)
            .with_placement_hint(0x23)
            .with_callsite(0xA110_5106);

        __unialloc_semantic_scope_push_hints_local(
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.lifetime_hint,
            metadata.placement_hint,
            metadata.callsite,
        );
        assert_eq!(
            active_allocation_metadata(),
            Some(metadata.with_placement_hint(
                metadata.placement_hint | PLACEMENT_HINT_LOCAL_SCOPE_NO_RECOVERY
            )),
            "local hint ABI must preserve hints while setting the no-recovery bit"
        );
        __unialloc_semantic_scope_pop();
        assert_eq!(active_allocation_metadata(), None);
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
    }

    #[test]
    fn ffi_scope_push_pop_unknown_base_uses_empty_fast_restore() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
            SEMANTIC_SCOPE_STACK[0] = AllocationMetadata::for_type(0xBAD0);
        }
        let metadata = AllocationMetadata::for_type(0xC002_5103)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_callsite(0xA110_5103);

        __unialloc_semantic_scope_push(
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.callsite,
        );
        assert_eq!(active_allocation_metadata(), Some(metadata));
        __unialloc_semantic_scope_pop();
        assert_eq!(
            active_allocation_metadata(),
            None,
            "unknown-base fast restore should not read a stale stack slot"
        );

        unsafe {
            reset_semantic_scope_stack_for_test();
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn ffi_scope_push_overflow_preserves_overflow_scope_and_routes_allocations() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();

        for index in 0..SEMANTIC_SCOPE_STACK_CAPACITY {
            let metadata = AllocationMetadata::for_type(0xC002_6000 + index as u64)
                .with_flags(FLAG_TYPE_ISOLATED)
                .with_callsite(0xA110_6000 + index as u64);
            __unialloc_semantic_scope_push(
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            );
            assert_eq!(active_allocation_metadata(), Some(metadata));
        }

        let overflow_metadata = AllocationMetadata::for_type(0xC002_6FFF)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_callsite(0xA110_6FFF);
        __unialloc_semantic_scope_push(
            overflow_metadata.type_id,
            overflow_metadata.module_id,
            overflow_metadata.flags,
            overflow_metadata.callsite,
        );
        assert_eq!(
            active_allocation_metadata(),
            Some(overflow_metadata),
            "bounded overflow scopes must still become the active typed scope"
        );

        let second_overflow_metadata = AllocationMetadata::for_type(0xC002_7000)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_callsite(0xA110_7000);
        __unialloc_semantic_scope_push(
            second_overflow_metadata.type_id,
            second_overflow_metadata.module_id,
            second_overflow_metadata.flags,
            second_overflow_metadata.callsite,
        );
        assert_eq!(active_allocation_metadata(), Some(second_overflow_metadata));
        let overflow_snapshot = semantic_scope_depth_snapshot();
        assert_eq!(overflow_snapshot.main_depth, SEMANTIC_SCOPE_STACK_CAPACITY);
        assert_eq!(overflow_snapshot.overflow_depth, 2);
        assert_eq!(
            overflow_snapshot.represented_depth,
            SEMANTIC_SCOPE_STACK_CAPACITY + 2
        );
        assert!(
            overflow_snapshot.active_overflow_scope_represented,
            "the active overflow scope should be represented by the bounded overflow stack"
        );
        let mut ffi_snapshot = SemanticScopeDepthSnapshot {
            main_depth: 0,
            overflow_depth: 0,
            overflow_capacity: 0,
            represented_depth: 0,
            active_overflow_scope_represented: false,
        };
        assert!(unsafe { __unialloc_semantic_scope_depth_snapshot(&mut ffi_snapshot) });
        assert_eq!(ffi_snapshot, overflow_snapshot);
        unsafe {
            assert_eq!(
                semantic_scope_stack_depth_for_test(),
                SEMANTIC_SCOPE_STACK_CAPACITY
            );
            assert_eq!(semantic_scope_stack_overflow_depth_for_test(), 2);
        }

        let second_ptr = unsafe { alloc.alloc(layout) };
        assert!(!second_ptr.is_null());
        unsafe {
            alloc.dealloc(second_ptr, layout);
        }
        assert_eq!(
            semantic_stats_snapshot().last_type_id,
            second_overflow_metadata.type_id,
            "real GlobalAlloc traffic inside an overflow scope must use that overflow metadata"
        );

        __unialloc_semantic_scope_pop();
        assert_eq!(
            active_allocation_metadata(),
            Some(overflow_metadata),
            "popping the inner overflow scope must restore the previous overflow scope"
        );
        let overflow_snapshot = semantic_scope_depth_snapshot();
        assert_eq!(overflow_snapshot.overflow_depth, 1);
        assert_eq!(
            overflow_snapshot.represented_depth,
            SEMANTIC_SCOPE_STACK_CAPACITY + 1
        );
        assert!(overflow_snapshot.active_overflow_scope_represented);

        let first_ptr = unsafe { alloc.alloc(layout) };
        assert!(!first_ptr.is_null());
        unsafe {
            alloc.dealloc(first_ptr, layout);
        }
        let snap = semantic_stats_snapshot();
        assert_eq!(snap.total_allocations, 2);
        assert_eq!(snap.typed_allocations, 2);
        assert_eq!(snap.typed_deallocations, 2);
        assert_eq!(
            snap.last_type_id, overflow_metadata.type_id,
            "outer overflow scope must route subsequent real allocations"
        );
        unsafe {
            assert_eq!(
                semantic_scope_stack_depth_for_test(),
                SEMANTIC_SCOPE_STACK_CAPACITY
            );
            assert_eq!(semantic_scope_stack_overflow_depth_for_test(), 1);
        }

        __unialloc_semantic_scope_pop();
        assert_eq!(
            active_allocation_metadata().unwrap().type_id,
            0xC002_6000 + (SEMANTIC_SCOPE_STACK_CAPACITY - 1) as u64,
            "matching overflow pop must leave the main scope stack untouched"
        );
        unsafe {
            assert_eq!(
                semantic_scope_stack_depth_for_test(),
                SEMANTIC_SCOPE_STACK_CAPACITY
            );
            assert_eq!(semantic_scope_stack_overflow_depth_for_test(), 0);
        }

        for expected in (1..SEMANTIC_SCOPE_STACK_CAPACITY).rev() {
            __unialloc_semantic_scope_pop();
            assert_eq!(
                active_allocation_metadata().unwrap().type_id,
                0xC002_6000 + (expected - 1) as u64
            );
        }
        __unialloc_semantic_scope_pop();
        assert_eq!(active_allocation_metadata(), None);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, overflow_metadata);
            drain_semantic_cache_for_test(&alloc, layout, second_overflow_metadata);
            reset_semantic_scope_stack_for_test();
        }
    }
    #[cfg(feature = "stats")]
    #[test]
    fn ffi_scope_push_beyond_overflow_capacity_falls_back_until_representable_pop() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
            clear_type_cache_for_test();
            clear_delayed_free_for_test();
        }
        semantic_auto_metadata_disable();
        semantic_stats_reset();

        let alloc = RustAllocator::new();
        let layout = Layout::from_size_align(64, align_of::<usize>()).unwrap();

        for index in 0..SEMANTIC_SCOPE_STACK_CAPACITY {
            let metadata = AllocationMetadata::for_type(0xC002_8000 + index as u64)
                .with_flags(FLAG_TYPE_ISOLATED)
                .with_callsite(0xA110_8000 + index as u64);
            __unialloc_semantic_scope_push(
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            );
        }

        let mut last_represented = AllocationMetadata::unknown();
        for index in 0..SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY {
            let metadata = AllocationMetadata::for_type(0xC003_0000 + index as u64)
                .with_flags(FLAG_TYPE_ISOLATED)
                .with_callsite(0xA111_0000 + index as u64);
            __unialloc_semantic_scope_push(
                metadata.type_id,
                metadata.module_id,
                metadata.flags,
                metadata.callsite,
            );
            last_represented = metadata;
        }
        assert_eq!(active_allocation_metadata(), Some(last_represented));

        let unrepresented = AllocationMetadata::for_type(0xC003_FFFF)
            .with_flags(FLAG_TYPE_ISOLATED)
            .with_callsite(0xA111_FFFF);
        __unialloc_semantic_scope_push(
            unrepresented.type_id,
            unrepresented.module_id,
            unrepresented.flags,
            unrepresented.callsite,
        );
        assert_eq!(
            active_allocation_metadata(),
            None,
            "unrepresentable overflow scopes must not inherit the last representable metadata"
        );
        let saturated_snapshot = semantic_scope_depth_snapshot();
        assert_eq!(
            saturated_snapshot.overflow_depth,
            SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY + 1
        );
        assert_eq!(
            saturated_snapshot.represented_depth,
            SEMANTIC_SCOPE_STACK_CAPACITY + SEMANTIC_SCOPE_OVERFLOW_STACK_CAPACITY
        );
        assert!(!saturated_snapshot.active_overflow_scope_represented);

        let fallback_ptr = unsafe { alloc.alloc(layout) };
        assert!(!fallback_ptr.is_null());
        unsafe {
            alloc.dealloc(fallback_ptr, layout);
        }
        let fallback_snap = semantic_stats_snapshot();
        assert_eq!(fallback_snap.total_allocations, 1);
        assert_eq!(fallback_snap.typed_allocations, 0);
        assert_eq!(fallback_snap.fallback_allocations, 1);
        assert_eq!(fallback_snap.fallback_deallocations, 1);

        __unialloc_semantic_scope_pop();
        assert_eq!(
            active_allocation_metadata(),
            Some(last_represented),
            "popping back to the representable boundary must restore the exact boundary scope"
        );
        let represented_ptr = unsafe { alloc.alloc(layout) };
        assert!(!represented_ptr.is_null());
        unsafe {
            alloc.dealloc(represented_ptr, layout);
        }
        let represented_snap = semantic_stats_snapshot();
        assert_eq!(represented_snap.total_allocations, 2);
        assert_eq!(represented_snap.typed_allocations, 1);
        assert_eq!(represented_snap.fallback_allocations, 1);
        assert_eq!(represented_snap.typed_deallocations, 1);
        assert_eq!(represented_snap.last_type_id, last_represented.type_id);

        semantic_stats_recording_disable();
        unsafe {
            drain_semantic_cache_for_test(&alloc, layout, last_represented);
            reset_semantic_scope_stack_for_test();
        }
    }

    #[test]
    fn scoped_metadata_restores_after_panic() {
        let _guard = test_guard();
        unsafe {
            reset_semantic_scope_stack_for_test();
        }
        let metadata = AllocationMetadata::for_type(0xC002_0BAD).with_flags(FLAG_TYPE_ISOLATED);

        let result = std::panic::catch_unwind(|| {
            with_semantic_metadata(metadata, || {
                assert_eq!(
                    active_allocation_metadata().unwrap().type_id,
                    metadata.type_id
                );
                panic!("semantic metadata scope panic");
            });
        });

        assert!(result.is_err());
        assert_eq!(active_allocation_metadata(), None);
    }

    #[test]
    fn ffi_auto_metadata_enable_disable_toggles_global_mode() {
        let _guard = test_guard();
        __unialloc_semantic_auto_metadata_disable();
        assert!(!semantic_auto_metadata_enabled());
        assert!(__unialloc_semantic_auto_metadata_enable(9, 0, 10));
        assert!(semantic_auto_metadata_enabled());
        let layout = Layout::from_size_align(8, align_of::<usize>()).unwrap();
        assert!(auto_allocation_metadata(layout)
            .unwrap()
            .requests(FLAG_TYPE_ISOLATED));
        __unialloc_semantic_auto_metadata_disable();
        assert!(!semantic_auto_metadata_enabled());
    }

    #[test]
    fn ffi_compiler_auto_metadata_enable_disable_toggles_replay_mode() {
        let _guard = test_guard();
        let ids = [0xC002_0101_u64, 0xC002_0102_u64];
        let layout = Layout::from_size_align(8, align_of::<usize>()).unwrap();

        __unialloc_semantic_auto_metadata_disable();
        assert!(!semantic_auto_compiler_metadata_enabled());
        assert!(unsafe {
            __unialloc_semantic_auto_compiler_metadata_enable(9, 0, 10, ids.as_ptr(), ids.len())
        });
        assert!(semantic_auto_compiler_metadata_enabled());
        assert_eq!(auto_allocation_metadata(layout).unwrap().type_id, ids[0]);
        __unialloc_semantic_auto_metadata_disable();
        assert!(!semantic_auto_compiler_metadata_enabled());
    }

    #[test]
    fn ffi_compiler_auto_metadata_enable_disable_toggles_stream_mode() {
        let _guard = test_guard();
        let ids = [0xC002_0201_u64];
        let layout = Layout::from_size_align(8, align_of::<usize>()).unwrap();

        __unialloc_semantic_auto_metadata_disable();
        assert!(!semantic_auto_compiler_metadata_stream_enabled());
        assert!(unsafe {
            __unialloc_semantic_auto_compiler_metadata_stream_enable(
                9,
                0,
                10,
                ids.as_ptr(),
                ids.len(),
            )
        });
        assert!(semantic_auto_compiler_metadata_enabled());
        assert!(semantic_auto_compiler_metadata_stream_enabled());
        assert_eq!(auto_allocation_metadata(layout).unwrap().type_id, ids[0]);
        assert_eq!(auto_allocation_metadata(layout), None);
        __unialloc_semantic_auto_metadata_disable();
        assert!(!semantic_auto_compiler_metadata_enabled());
        assert!(!semantic_auto_compiler_metadata_stream_enabled());
    }
}
