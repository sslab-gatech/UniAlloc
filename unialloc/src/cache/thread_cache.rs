//! Linklist based thread local cache
use crate::error::{AllocError, Result};
use crate::mm::linklist::Linklist;
use crate::sc::MetadataAllocator;
use crate::sc::META_BUMP;
use crate::size_class::*;
use crate::zone::try_global_zone;
use crate::*;
use alloc::boxed::Box;
use core::cell::RefCell;
use core::mem::align_of;
use core::ptr::null_mut;
#[cfg(any(feature = "stats", all(test, windows)))]
use core::sync::atomic::{AtomicUsize, Ordering};
use core::{
    alloc::{Allocator, GlobalAlloc, Layout},
    ptr::NonNull,
};

include!(concat!(env!("OUT_DIR"), "/sizeclass_consts.rs"));

/// Per-size-class soft cap for a thread-local free list.
///
/// The cache is intentionally thread-local, so allocation/deallocation on the
/// hot path does not take a global lock.  The downside of a thread-local cache
/// is retained memory: every thread can keep objects that are no longer useful
/// to other threads.  The old threshold was 256MiB *per size class*, which made
/// tiny objects effectively unbounded (for 8-byte objects, tens of millions of
/// cached nodes before a flush).
///
/// Keep the fast lock-free local reuse path, but bound retained memory.  A size
/// class flushes once it keeps roughly 256KiB of objects or 4096 objects,
/// whichever comes first.  After a flush, the cache keeps a smaller hot set
/// (see `THREAD_CACHE_TARGET_*`) and returns the suffix to the slab allocator.
const THREAD_CACHE_FLUSH_BYTES: usize = 256 * 1024;
const THREAD_CACHE_TARGET_BYTES: usize = 64 * 1024;
const THREAD_CACHE_FLUSH_OBJECTS_MAX: usize = 4096;
const THREAD_CACHE_TARGET_OBJECTS_MAX: usize = 1024;
/// Hot-prefix cap kept after a strict-alignment miss.
///
/// A normal soft flush keeps up to 1024 tiny objects because they are known to
/// be locally reusable.  After a high-alignment miss, however, the scanned
/// objects have just proven useless for the current request.  Keeping a large
/// lower-aligned list would make the next strict request rescan the same cold
/// nodes and would pin objects that another thread could use.  Retain a small
/// prefix for ordinary follow-up allocations and return the cold suffix to the
/// slab.
const THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX: usize = 32;
/// Byte cap kept after a strict-alignment miss.
///
/// Object-count caps preserve tiny-object compatibility, but larger rounded
/// size classes can retain tens of KiB of nodes that just failed the strict
/// alignment request.  Bound that cold prefix by bytes too.
const THREAD_CACHE_ALIGNMENT_MISS_TARGET_BYTES: usize = 16 * 1024;
/// Hot-prefix cap kept after repeated strict-alignment misses.
///
/// The first miss preserves the historical 32-object compatibility target.
/// A second consecutive miss means even that hot prefix did not help any local
/// allocation, so retain only a minimal ordinary-allocation buffer before
/// returning the cold suffix to the slab.
const THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_OBJECTS_MAX: usize = 4;
/// Byte cap kept after repeated strict-alignment misses.
const THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_BYTES: usize = 4 * 1024;
/// Aggregate soft cap for all local free objects retained by one thread cache.
///
/// Per-size-class caps bound a single hot class, but they still allow every
/// class to sit just below its own threshold.  With the hosted TC table that is
/// roughly 16MiB per idle thread.  Keep a small per-thread hot set instead:
/// once retained object bytes cross 2MiB, return cold suffixes from the largest
/// classes until the cache is back near 1MiB.
const THREAD_CACHE_TOTAL_FLUSH_BYTES: usize = 2 * 1024 * 1024;
const THREAD_CACHE_TOTAL_TARGET_BYTES: usize = 1024 * 1024;
/// Dense cross-class aggregate soft cap for one thread cache.
///
/// The normal 2MiB/1MiB aggregate budget protects sparse or single-hot-class
/// workloads from needless churn.  When many size classes are simultaneously
/// non-empty, however, each class can hold a small private cold suffix and the
/// aggregate residue becomes cross-class retained bytes / external
/// fragmentation.  Use a tighter soft target only in that dense case.
const THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES: usize = 1024 * 1024;
const THREAD_CACHE_TOTAL_DENSE_TARGET_BYTES: usize = 512 * 1024;
const THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD: usize = 8;
/// A contested slab lock should not make every soft flush block the mutator.
///
/// Once the local list grows past this multiple of the soft cap we do allow one
/// blocking flush.  That keeps the common case non-blocking while preserving a
/// hard retention bound if a size class stays hot and contended.
const THREAD_CACHE_BLOCKING_FLUSH_MULTIPLIER: usize = 2;
const THREAD_CACHE_TOTAL_BLOCKING_FLUSH_MULTIPLIER: usize = 2;

/// Exact, read-only diagnostic for one thread cache's retained-object budget.
///
/// This snapshot deliberately recomputes list state instead of trusting the
/// maintained counters.  That makes it useful for footprint/debug gates: callers
/// can distinguish "the cache is within budget" from "the accounting drifted".
/// The diagnostic does not add per-object or per-free metadata to the hot path.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ThreadCacheFootprintSnapshot {
    /// Whether the backing thread cache exists.  Non-fixed builds always report
    /// true; fixed-heap builds can query before `init_tcache` installs storage.
    pub cache_initialized: bool,
    /// Maintained retained-byte counter used by the allocator hot path.
    pub cached_object_bytes: usize,
    /// Retained bytes recomputed by walking local lists and bump batches.
    pub exact_cached_object_bytes: usize,
    /// Maintained number of non-empty cached size classes.
    pub active_cached_classes: usize,
    /// Non-empty cached size classes recomputed from the lists.
    pub exact_active_cached_classes: usize,
    /// Size-class index with the largest retained byte count, or `usize::MAX`
    /// when the cache is empty.
    pub largest_retained_class_idx: usize,
    /// Retained bytes in `largest_retained_class_idx`.
    pub largest_retained_class_bytes: usize,
    /// Aggregate soft-flush threshold for the current class-density state.
    pub flush_bytes: usize,
    /// Aggregate post-trim target for the current class-density state.
    pub target_bytes: usize,
    /// Aggregate hard/blocking-flush threshold.
    pub hard_flush_bytes: usize,
    /// True when maintained counters match the exact recomputation.
    pub accounting_matches_exact: bool,
    /// True when exact retained bytes are above the aggregate soft cap.
    pub over_soft_budget: bool,
    /// True when exact retained bytes are above the aggregate target cap.
    pub over_target_budget: bool,
}

#[cfg(feature = "stats")]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ThreadCacheFlushStatsSnapshot {
    pub try_lock_attempts: usize,
    pub busy_no_transfer: usize,
    pub geometric_soft_skips: usize,
    pub hard_fallback_attempts: usize,
    pub returned_to_slab: usize,
    pub backend_errors: usize,
    pub restored_after_busy: usize,
    pub restore_failures: usize,
    pub uncertain_suffix_drops: usize,
}

#[cfg(feature = "stats")]
struct ThreadCacheFlushStats {
    try_lock_attempts: AtomicUsize,
    busy_no_transfer: AtomicUsize,
    geometric_soft_skips: AtomicUsize,
    hard_fallback_attempts: AtomicUsize,
    returned_to_slab: AtomicUsize,
    backend_errors: AtomicUsize,
    restored_after_busy: AtomicUsize,
    restore_failures: AtomicUsize,
    uncertain_suffix_drops: AtomicUsize,
}

#[cfg(feature = "stats")]
impl ThreadCacheFlushStats {
    const fn new() -> Self {
        Self {
            try_lock_attempts: AtomicUsize::new(0),
            busy_no_transfer: AtomicUsize::new(0),
            geometric_soft_skips: AtomicUsize::new(0),
            hard_fallback_attempts: AtomicUsize::new(0),
            returned_to_slab: AtomicUsize::new(0),
            backend_errors: AtomicUsize::new(0),
            restored_after_busy: AtomicUsize::new(0),
            restore_failures: AtomicUsize::new(0),
            uncertain_suffix_drops: AtomicUsize::new(0),
        }
    }

    fn snapshot(&self) -> ThreadCacheFlushStatsSnapshot {
        ThreadCacheFlushStatsSnapshot {
            try_lock_attempts: self.try_lock_attempts.load(Ordering::Relaxed),
            busy_no_transfer: self.busy_no_transfer.load(Ordering::Relaxed),
            geometric_soft_skips: self.geometric_soft_skips.load(Ordering::Relaxed),
            hard_fallback_attempts: self.hard_fallback_attempts.load(Ordering::Relaxed),
            returned_to_slab: self.returned_to_slab.load(Ordering::Relaxed),
            backend_errors: self.backend_errors.load(Ordering::Relaxed),
            restored_after_busy: self.restored_after_busy.load(Ordering::Relaxed),
            restore_failures: self.restore_failures.load(Ordering::Relaxed),
            uncertain_suffix_drops: self.uncertain_suffix_drops.load(Ordering::Relaxed),
        }
    }

    fn reset(&self) {
        self.try_lock_attempts.store(0, Ordering::Relaxed);
        self.busy_no_transfer.store(0, Ordering::Relaxed);
        self.geometric_soft_skips.store(0, Ordering::Relaxed);
        self.hard_fallback_attempts.store(0, Ordering::Relaxed);
        self.returned_to_slab.store(0, Ordering::Relaxed);
        self.backend_errors.store(0, Ordering::Relaxed);
        self.restored_after_busy.store(0, Ordering::Relaxed);
        self.restore_failures.store(0, Ordering::Relaxed);
        self.uncertain_suffix_drops.store(0, Ordering::Relaxed);
    }
}

#[cfg(feature = "stats")]
static THREAD_CACHE_FLUSH_STATS: ThreadCacheFlushStats = ThreadCacheFlushStats::new();

#[cfg(feature = "stats")]
#[inline]
fn thread_cache_flush_stat(counter: &AtomicUsize) {
    counter.fetch_add(1, Ordering::Relaxed);
}

#[cfg(feature = "stats")]
pub fn thread_cache_flush_stats_snapshot() -> ThreadCacheFlushStatsSnapshot {
    THREAD_CACHE_FLUSH_STATS.snapshot()
}

#[cfg(feature = "stats")]
pub fn thread_cache_flush_stats_reset() {
    THREAD_CACHE_FLUSH_STATS.reset();
}

#[inline]
fn thread_cache_should_flush(length: usize, rounded_size: usize) -> bool {
    length > 1
        && rounded_size != 0
        && length
            > thread_cache_object_limit(
                rounded_size,
                THREAD_CACHE_FLUSH_BYTES,
                THREAD_CACHE_FLUSH_OBJECTS_MAX,
            )
}

#[inline]
fn thread_cache_should_blocking_flush(length: usize, rounded_size: usize) -> bool {
    let soft_limit = thread_cache_object_limit(
        rounded_size,
        THREAD_CACHE_FLUSH_BYTES,
        THREAD_CACHE_FLUSH_OBJECTS_MAX,
    );
    let hard_limit = soft_limit.saturating_mul(THREAD_CACHE_BLOCKING_FLUSH_MULTIPLIER);
    length > hard_limit
}

#[inline]
fn thread_cache_soft_flush_attempt_due(length: usize, rounded_size: usize) -> bool {
    if !thread_cache_should_flush(length, rounded_size) {
        return false;
    }
    if thread_cache_should_blocking_flush(length, rounded_size) {
        return true;
    }

    let soft_limit = thread_cache_object_limit(
        rounded_size,
        THREAD_CACHE_FLUSH_BYTES,
        THREAD_CACHE_FLUSH_OBJECTS_MAX,
    );
    let overflow = length.saturating_sub(soft_limit);
    overflow.is_power_of_two()
}

#[inline]
fn thread_cache_object_limit(
    rounded_size: usize,
    byte_budget: usize,
    object_budget: usize,
) -> usize {
    if rounded_size == 0 {
        return usize::MAX;
    }
    let by_bytes = byte_budget / rounded_size;
    let at_least_one = if by_bytes == 0 { 1 } else { by_bytes };
    core::cmp::min(at_least_one, object_budget.max(1))
}

#[inline]
fn thread_cache_alignment_miss_object_limit(
    rounded_size: usize,
    byte_budget: usize,
    object_budget: usize,
) -> usize {
    if rounded_size == 0 {
        return usize::MAX;
    }
    core::cmp::min(byte_budget / rounded_size, object_budget)
}

#[inline]
fn thread_cache_target_keep_length(length: usize, rounded_size: usize) -> usize {
    if length <= 1 || rounded_size == 0 {
        return length;
    }
    let target = thread_cache_object_limit(
        rounded_size,
        THREAD_CACHE_TARGET_BYTES,
        THREAD_CACHE_TARGET_OBJECTS_MAX,
    );
    core::cmp::min(target, length - 1)
}

#[inline]
fn thread_cache_aggregate_pressure_keep_length(
    current_total: usize,
    target_total: usize,
    length: usize,
    rounded_size: usize,
) -> usize {
    if current_total <= target_total || length == 0 || rounded_size == 0 {
        return length;
    }

    let excess = current_total - target_total;
    let objects_to_return =
        excess.checked_add(rounded_size - 1).unwrap_or(usize::MAX) / rounded_size;
    length.saturating_sub(core::cmp::min(objects_to_return, length))
}

#[inline]
fn thread_cache_total_pressure_budget(active_cached_classes: usize) -> (usize, usize) {
    if active_cached_classes >= THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD {
        (
            THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES,
            THREAD_CACHE_TOTAL_DENSE_TARGET_BYTES,
        )
    } else {
        (
            THREAD_CACHE_TOTAL_FLUSH_BYTES,
            THREAD_CACHE_TOTAL_TARGET_BYTES,
        )
    }
}

#[inline]
fn thread_cache_total_blocking_flush_limit(active_cached_classes: usize) -> usize {
    let (flush_bytes, _target_bytes) = thread_cache_total_pressure_budget(active_cached_classes);
    flush_bytes.saturating_mul(THREAD_CACHE_TOTAL_BLOCKING_FLUSH_MULTIPLIER)
}

#[inline]
fn thread_cache_alignment_miss_keep_length(length: usize, rounded_size: usize) -> usize {
    let normal_target = thread_cache_target_keep_length(length, rounded_size);
    let alignment_target = thread_cache_alignment_miss_object_limit(
        rounded_size,
        THREAD_CACHE_ALIGNMENT_MISS_TARGET_BYTES,
        THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
    );
    core::cmp::min(normal_target, alignment_target)
}

#[inline]
fn thread_cache_repeated_alignment_miss_keep_length(
    length: usize,
    rounded_size: usize,
    alignment_miss_streak: u8,
) -> usize {
    let first_miss_target = thread_cache_alignment_miss_keep_length(length, rounded_size);
    if alignment_miss_streak <= 1 {
        first_miss_target
    } else {
        let repeated_target = thread_cache_alignment_miss_object_limit(
            rounded_size,
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_BYTES,
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
        );
        core::cmp::min(first_miss_target, repeated_target)
    }
}

#[inline]
fn thread_cache_should_bypass_local_cache(idx: usize, rounded_size: usize) -> bool {
    if idx == 0 || idx >= TOTAL_SIZE_CLASS || rounded_size == 0 {
        return false;
    }

    let pages = get_num_pages_by_idx(idx);
    matches!(
        crate::sc::checked_size_class_geometry(rounded_size, pages),
        Some((slot_count, _stride)) if slot_count <= 1
    )
}

const ALIGNMENT_MISS_STREAK_BITS: usize = 2;
const ALIGNMENT_MISS_STREAK_MASK: usize = (1 << ALIGNMENT_MISS_STREAK_BITS) - 1;
const ALIGNMENT_MISS_STREAKS_PER_WORD: usize = usize::BITS as usize / ALIGNMENT_MISS_STREAK_BITS;
const ALIGNMENT_MISS_STREAK_WORDS: usize =
    (TOTAL_SIZE_CLASS + ALIGNMENT_MISS_STREAKS_PER_WORD - 1) / ALIGNMENT_MISS_STREAKS_PER_WORD;

const ACTIVE_CACHED_CLASS_BITS_PER_WORD: usize = usize::BITS as usize;
const ACTIVE_CACHED_CLASS_WORDS: usize =
    (TOTAL_SIZE_CLASS + ACTIVE_CACHED_CLASS_BITS_PER_WORD - 1) / ACTIVE_CACHED_CLASS_BITS_PER_WORD;

/// Compact bitmap of size classes that currently retain local objects.
///
/// `active_cached_classes` is enough to choose the sparse/dense budget, but the
/// aggregate trim path also needs to find which classes to visit.  Keeping one
/// bit per class lets sparse workloads skip empty gaps without adding a
/// per-class byte counter or a larger side table.  Class zero is intentionally
/// ignored because zero-sized allocations do not retain cacheable objects.
#[derive(Clone, Copy)]
struct ActiveCachedClassBits {
    words: [usize; ACTIVE_CACHED_CLASS_WORDS],
}

impl ActiveCachedClassBits {
    const fn new() -> Self {
        Self {
            words: [0; ACTIVE_CACHED_CLASS_WORDS],
        }
    }

    #[inline]
    const fn word_and_bit(idx: usize) -> (usize, usize) {
        (
            idx / ACTIVE_CACHED_CLASS_BITS_PER_WORD,
            idx % ACTIVE_CACHED_CLASS_BITS_PER_WORD,
        )
    }

    #[inline]
    fn set_present(&mut self, idx: usize, present: bool) -> bool {
        if idx == 0 || idx >= TOTAL_SIZE_CLASS {
            return false;
        }
        let (word, bit) = Self::word_and_bit(idx);
        let mask = 1usize << bit;
        let was_present = (self.words[word] & mask) != 0;
        if present {
            self.words[word] |= mask;
        } else {
            self.words[word] &= !mask;
        }
        was_present != present
    }

    #[inline]
    fn is_present(&self, idx: usize) -> bool {
        if idx == 0 || idx >= TOTAL_SIZE_CLASS {
            return false;
        }
        let (word, bit) = Self::word_and_bit(idx);
        (self.words[word] & (1usize << bit)) != 0
    }

    #[inline]
    fn low_bits_through(bit: usize) -> usize {
        if bit + 1 >= ACTIVE_CACHED_CLASS_BITS_PER_WORD {
            usize::MAX
        } else {
            (1usize << (bit + 1)) - 1
        }
    }

    #[inline]
    fn previous_at_or_below(&self, start_idx: usize) -> Option<usize> {
        if TOTAL_SIZE_CLASS <= 1 {
            return None;
        }
        let bounded_start = core::cmp::min(start_idx, TOTAL_SIZE_CLASS - 1);
        if bounded_start == 0 {
            return None;
        }
        let (mut word_idx, bit) = Self::word_and_bit(bounded_start);
        let mut word = self.words[word_idx] & Self::low_bits_through(bit);

        loop {
            if word != 0 {
                let bit = ACTIVE_CACHED_CLASS_BITS_PER_WORD - 1 - word.leading_zeros() as usize;
                let idx = word_idx * ACTIVE_CACHED_CLASS_BITS_PER_WORD + bit;
                if idx != 0 && idx < TOTAL_SIZE_CLASS {
                    return Some(idx);
                }
                word &= !(1usize << bit);
                continue;
            }
            if word_idx == 0 {
                return None;
            }
            word_idx -= 1;
            word = self.words[word_idx];
        }
    }
}

/// Compact per-class strict-alignment miss state.
///
/// Each class gets a 2-bit saturating counter: 0=no miss, 1=first miss,
/// 2=repeated miss.  Keeping this beside `ThreadCache` avoids padding every
/// `ThreadCacheUnit` while preserving per-class repeated-miss behavior.
#[derive(Clone, Copy)]
struct AlignmentMissStreaks {
    words: [usize; ALIGNMENT_MISS_STREAK_WORDS],
}

impl AlignmentMissStreaks {
    const fn new() -> Self {
        Self {
            words: [0; ALIGNMENT_MISS_STREAK_WORDS],
        }
    }

    #[inline]
    const fn word_and_shift(idx: usize) -> (usize, usize) {
        let word = idx / ALIGNMENT_MISS_STREAKS_PER_WORD;
        let shift = (idx % ALIGNMENT_MISS_STREAKS_PER_WORD) * ALIGNMENT_MISS_STREAK_BITS;
        (word, shift)
    }

    #[inline]
    fn get(&self, idx: usize) -> u8 {
        if idx >= TOTAL_SIZE_CLASS {
            return 0;
        }
        let (word, shift) = Self::word_and_shift(idx);
        ((self.words[word] >> shift) & ALIGNMENT_MISS_STREAK_MASK) as u8
    }

    #[inline]
    fn set(&mut self, idx: usize, streak: u8) {
        if idx >= TOTAL_SIZE_CLASS {
            return;
        }
        let (word, shift) = Self::word_and_shift(idx);
        let value = core::cmp::min(streak, 2) as usize;
        self.words[word] =
            (self.words[word] & !(ALIGNMENT_MISS_STREAK_MASK << shift)) | (value << shift);
    }

    #[inline]
    fn record_miss(&mut self, idx: usize) -> u8 {
        if idx >= TOTAL_SIZE_CLASS {
            return 0;
        }
        let streak = core::cmp::min(self.get(idx).saturating_add(1), 2);
        self.set(idx, streak);
        streak
    }

    #[inline]
    fn reset(&mut self, idx: usize) {
        self.set(idx, 0);
    }
}

#[derive(Clone, Copy)]
struct ThreadCacheUnit {
    list: Linklist,
    bump_ptr: usize,
    bump_count: i32,
    bump_unit: i32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct DetachedFlushBatch {
    boundary: usize,
    suffix_head: usize,
    original_length: usize,
    kept_length: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FlushDisposition {
    ReturnedToSlab,
    BusyNoTransfer,
    FailedAfterTransferAttempt,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct TrimClassResult {
    reduced_cached_bytes: bool,
    busy_no_transfer: bool,
}

impl ThreadCacheUnit {
    pub const fn new() -> Self {
        Self {
            list: Linklist::new(),
            bump_ptr: 0,
            bump_count: 0,
            bump_unit: 0,
        }
    }

    fn clear_bump_batch(&mut self) {
        self.bump_ptr = 0;
        self.bump_count = 0;
        self.bump_unit = 0;
    }

    #[inline]
    fn bump_len(&self) -> usize {
        if self.bump_count > 0 {
            self.bump_count as usize
        } else {
            0
        }
    }

    /// Convert the contiguous bump tail into the normal local linked list.
    ///
    /// Bump batches are valid free objects already owned by this thread cache;
    /// they are just represented compactly as `(bump_ptr, bump_count,
    /// bump_unit)` instead of per-object links.  Draining them through
    /// `take_bump_slot` lets aggregate-pressure trimming hand the same cold
    /// objects back to the slab without adding a second flush path.
    fn drain_bump_batch_to_list(&mut self) {
        while self.bump_count > 0 {
            let bump_ptr = match self.take_bump_slot() {
                Ok(ptr) => ptr,
                Err(_) => break,
            };
            self.free(bump_ptr as *mut u8);
        }
    }

    fn take_bump_slot(&mut self) -> Result<usize> {
        if self.bump_count <= 0 {
            self.clear_bump_batch();
            return Err(AllocError::ESIZE);
        }
        if self.bump_ptr == 0 || self.bump_unit <= 0 {
            self.clear_bump_batch();
            return Err(AllocError::ESIZE);
        }

        let current = self.bump_ptr;
        let remaining = self.bump_count - 1;
        if remaining == 0 {
            self.clear_bump_batch();
            return Ok(current);
        }

        let next = match current.checked_add(self.bump_unit as usize) {
            Some(next) => next,
            None => {
                self.clear_bump_batch();
                return Err(AllocError::ESIZE);
            }
        };
        self.bump_ptr = next;
        self.bump_count = remaining;
        Ok(current)
    }

    pub fn clean_up(&mut self, idx: usize) {
        while self.bump_count > 0 {
            let bump_ptr = match self.take_bump_slot() {
                Ok(ptr) => ptr,
                Err(_) => break,
            };
            self.free(bump_ptr as *mut u8);
        }
        if self.list.length() == 0 {
            *self = Self::new();
            return;
        }
        if self.validate_local_list_exact().is_err() {
            self.clear_local_list();
            return;
        }
        if idx >= TOTAL_SIZE_CLASS {
            return;
        }
        let zone = match try_global_zone() {
            Ok(zone) => zone,
            Err(_) => return,
        };
        match zone.deallocate_batch_to_slab(idx, self.list.link as *mut u8) {
            Ok(()) => *self = Self::new(),
            Err(_) => {
                // The slab may have consumed a prefix of a multi-page list
                // before discovering backend corruption.  Do not leave the
                // original list cacheable in that uncertain-ownership state.
                self.clear_local_list();
            }
        }
    }

    fn free(&mut self, ptr: *mut u8) {
        self.list.push_unchecked(ptr);
    }

    #[inline]
    fn reset_alignment_miss_streak(alignment_miss_streak: &mut u8) {
        *alignment_miss_streak = 0;
    }

    #[inline]
    fn record_alignment_miss(alignment_miss_streak: &mut u8) {
        *alignment_miss_streak = core::cmp::min(alignment_miss_streak.saturating_add(1), 2);
    }

    pub fn deallocate(
        &mut self,
        idx: usize,
        ptr: NonNull<u8>,
        size: usize,
    ) -> Option<FlushDisposition> {
        self.list.push_unchecked(ptr.as_ptr());

        if idx >= TOTAL_SIZE_CLASS {
            return None;
        }
        if thread_cache_should_flush(self.list.length, size) {
            let blocking_flush = thread_cache_should_blocking_flush(self.list.length, size);
            if !blocking_flush && !thread_cache_soft_flush_attempt_due(self.list.length, size) {
                #[cfg(feature = "stats")]
                thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.geometric_soft_skips);
                return None;
            }
            let keep_length = thread_cache_target_keep_length(self.list.length, size);
            return Some(self.flush_suffix_to_slab(idx, keep_length, blocking_flush));
        }
        None
    }

    fn deallocate_direct_to_slab(&mut self, idx: usize, ptr: NonNull<u8>) -> FlushDisposition {
        self.list.push_unchecked(ptr.as_ptr());
        self.flush_suffix_to_slab(idx, 0, true)
    }

    /// Return the cold suffix of a local free list to the owning slab.
    ///
    /// The suffix is detached before handing it to the slab so the local hot
    /// prefix remains immediately reusable.  A busy non-blocking slab lock is
    /// the one case where ownership definitely did not transfer; we splice the
    /// suffix back.  Backend validation errors are different because the slab
    /// may have consumed part of a multi-page batch, so the detached suffix is
    /// treated as uncertain ownership and not re-cached.
    fn flush_suffix_to_slab(
        &mut self,
        idx: usize,
        keep_length: usize,
        blocking_flush: bool,
    ) -> FlushDisposition {
        if idx >= TOTAL_SIZE_CLASS {
            return FlushDisposition::BusyNoTransfer;
        }
        let zone = match try_global_zone() {
            Ok(zone) => zone,
            Err(_) => return FlushDisposition::BusyNoTransfer,
        };
        if !zone.contains_slab_index(idx) {
            return FlushDisposition::BusyNoTransfer;
        }
        let batch = match self.detach_flush_suffix(keep_length) {
            Ok(Some(batch)) => batch,
            Ok(None) => return FlushDisposition::BusyNoTransfer,
            Err(_) => {
                self.clear_local_list();
                return FlushDisposition::FailedAfterTransferAttempt;
            }
        };

        #[cfg(feature = "stats")]
        thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.try_lock_attempts);
        let flush_disposition =
            match zone.try_deallocate_batch_to_slab(idx, batch.suffix_head as *mut u8) {
                Ok(true) => {
                    #[cfg(feature = "stats")]
                    thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.returned_to_slab);
                    FlushDisposition::ReturnedToSlab
                }
                Ok(false) if blocking_flush => {
                    #[cfg(feature = "stats")]
                    {
                        thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.busy_no_transfer);
                        thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.hard_fallback_attempts);
                    }
                    match zone.deallocate_batch_to_slab(idx, batch.suffix_head as *mut u8) {
                        Ok(()) => {
                            #[cfg(feature = "stats")]
                            thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.returned_to_slab);
                            FlushDisposition::ReturnedToSlab
                        }
                        Err(_) => {
                            #[cfg(feature = "stats")]
                            thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.backend_errors);
                            FlushDisposition::FailedAfterTransferAttempt
                        }
                    }
                }
                Ok(false) => {
                    #[cfg(feature = "stats")]
                    thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.busy_no_transfer);
                    FlushDisposition::BusyNoTransfer
                }
                Err(_) => {
                    #[cfg(feature = "stats")]
                    thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.backend_errors);
                    FlushDisposition::FailedAfterTransferAttempt
                }
            };

        if flush_disposition == FlushDisposition::BusyNoTransfer {
            if self.restore_detached_flush_suffix(batch).is_err() {
                #[cfg(feature = "stats")]
                thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.restore_failures);
                self.clear_local_list();
                FlushDisposition::FailedAfterTransferAttempt
            } else {
                #[cfg(feature = "stats")]
                thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.restored_after_busy);
                FlushDisposition::BusyNoTransfer
            }
        } else {
            if flush_disposition == FlushDisposition::FailedAfterTransferAttempt {
                #[cfg(feature = "stats")]
                thread_cache_flush_stat(&THREAD_CACHE_FLUSH_STATS.uncertain_suffix_drops);
            }
            flush_disposition
        }
    }

    /// Trim cached objects that just failed a stricter alignment request.
    ///
    /// A high-alignment allocation can scan the whole local list and find no
    /// usable node even though the list still retains many lower-aligned
    /// objects.  Holding all of those objects locally makes the current thread
    /// ask the backend for more slab/page memory while other threads cannot use
    /// the retained objects.  After such a miss, return only the cold suffix
    /// above the normal hot-target budget and do it non-blockingly: if the slab
    /// lock is busy, the suffix is restored and the allocator proceeds.
    fn trim_after_recorded_alignment_miss(
        &mut self,
        idx: usize,
        rounded_size: usize,
        alignment_miss_streak: u8,
    ) -> FlushDisposition {
        if rounded_size == 0 || self.list.length == 0 {
            return FlushDisposition::BusyNoTransfer;
        }
        let keep_length = thread_cache_repeated_alignment_miss_keep_length(
            self.list.length,
            rounded_size,
            alignment_miss_streak,
        );
        if keep_length >= self.list.length {
            return FlushDisposition::BusyNoTransfer;
        }
        self.flush_suffix_to_slab(idx, keep_length, false)
    }

    fn trim_after_alignment_miss(
        &mut self,
        idx: usize,
        rounded_size: usize,
        alignment_miss_streak: &mut u8,
    ) -> FlushDisposition {
        Self::record_alignment_miss(alignment_miss_streak);
        self.trim_after_recorded_alignment_miss(idx, rounded_size, *alignment_miss_streak)
    }

    #[inline]
    fn non_null_chunk(ptr: *mut u8) -> Result<NonNull<u8>> {
        NonNull::new(ptr).ok_or(AllocError::ENOMEM)
    }

    fn read_link_word(addr: usize) -> Result<usize> {
        if addr == 0 || addr % align_of::<usize>() != 0 {
            return Err(AllocError::ESIZE);
        }
        Ok(unsafe { *(addr as *const usize) })
    }

    fn validate_exact_link_chain(head: usize, expected_nodes: usize) -> Result<usize> {
        if expected_nodes == 0 || head == 0 {
            return Err(AllocError::ESIZE);
        }

        let mut cur = head;
        for _ in 1..expected_nodes {
            let next = Self::read_link_word(cur)?;
            if next == 0 || next % align_of::<usize>() != 0 {
                return Err(AllocError::ESIZE);
            }
            cur = next;
        }

        if Self::read_link_word(cur)? != 0 {
            return Err(AllocError::ESIZE);
        }
        Ok(cur)
    }

    fn validate_local_list_exact(&self) -> Result<()> {
        match (self.list.link, self.list.length) {
            (0, 0) => Ok(()),
            (0, _) => Err(AllocError::ESIZE),
            (head, nodes) => Self::validate_exact_link_chain(head, nodes).map(|_| ()),
        }
    }

    fn detach_flush_suffix(&mut self, kept_length: usize) -> Result<Option<DetachedFlushBatch>> {
        let original_length = self.list.length;
        if kept_length >= original_length {
            return Ok(None);
        }
        if self.list.link == 0 || self.list.link % align_of::<usize>() != 0 {
            return Err(AllocError::ESIZE);
        }
        if kept_length == 0 {
            let suffix_head = self.list.link;
            Self::validate_exact_link_chain(suffix_head, original_length)?;
            self.clear_local_list();
            return Ok(Some(DetachedFlushBatch {
                boundary: 0,
                suffix_head,
                original_length,
                kept_length,
            }));
        }

        let mut boundary = self.list.link;
        for _ in 1..kept_length {
            let next = Self::read_link_word(boundary)?;
            if next == 0 || next % align_of::<usize>() != 0 {
                return Err(AllocError::ESIZE);
            }
            boundary = next;
        }

        let suffix_head = Self::read_link_word(boundary)?;
        if suffix_head == 0 || suffix_head % align_of::<usize>() != 0 {
            return Err(AllocError::ESIZE);
        }

        let suffix_length = original_length
            .checked_sub(kept_length)
            .ok_or(AllocError::ESIZE)?;
        Self::validate_exact_link_chain(suffix_head, suffix_length)?;

        unsafe {
            *(boundary as *mut usize) = 0;
        }
        self.list.length = kept_length;

        Ok(Some(DetachedFlushBatch {
            boundary,
            suffix_head,
            original_length,
            kept_length,
        }))
    }

    fn restore_detached_flush_suffix(&mut self, batch: DetachedFlushBatch) -> Result<()> {
        if batch.suffix_head == 0 || batch.suffix_head % align_of::<usize>() != 0 {
            return Err(AllocError::ESIZE);
        }
        if batch.kept_length == 0 {
            if self.list.link != 0 || self.list.length != 0 || batch.boundary != 0 {
                return Err(AllocError::ESIZE);
            }
            Self::validate_exact_link_chain(batch.suffix_head, batch.original_length)?;
            self.list.link = batch.suffix_head;
            self.list.length = batch.original_length;
            return Ok(());
        }
        if self.list.link == 0
            || self.list.length != batch.kept_length
            || batch.boundary == 0
            || batch.boundary % align_of::<usize>() != 0
        {
            return Err(AllocError::ESIZE);
        }
        unsafe {
            *(batch.boundary as *mut usize) = batch.suffix_head;
        }
        self.list.length = batch.original_length;
        Ok(())
    }

    fn clear_local_list(&mut self) {
        self.list = Linklist::new();
    }

    fn validate_linked_backend_tail(head: usize, remaining: usize) -> Result<(usize, usize)> {
        if remaining == 0 {
            return Ok((0, 0));
        }

        let first = Self::read_link_word(head)?;
        if first == 0 {
            return Err(AllocError::ESIZE);
        }

        let mut cur = first;
        let mut seen = 1usize;
        while seen < remaining {
            cur = Self::read_link_word(cur)?;
            if cur == 0 {
                return Err(AllocError::ESIZE);
            }
            seen += 1;
        }

        if Self::read_link_word(cur)? != 0 {
            return Err(AllocError::ESIZE);
        }

        Ok((first, cur))
    }

    fn install_linked_backend_tail(&mut self, head: NonNull<u8>, remaining: usize) -> Result<()> {
        let (tail_head, tail_last) =
            Self::validate_linked_backend_tail(head.as_ptr() as usize, remaining)?;
        let new_length = self
            .list
            .length
            .checked_add(remaining)
            .ok_or(AllocError::ESIZE)?;

        if (self.list.length == 0) != (self.list.link == 0) {
            return Err(AllocError::EFATAL);
        }

        let old_head = self.list.link;
        if old_head != 0 {
            // Backend refill happens after a local cache miss, so this is a cold
            // path.  Validate the existing local list before splicing the new
            // backend tail into it: keeping a corrupt or overlong list cached
            // would hide the ownership error until a later flush/deallocation.
            self.validate_local_list_exact()?;
            unsafe {
                *(tail_last as *mut usize) = old_head;
            }
        }
        self.list.link = tail_head;
        self.list.length = new_length;
        Ok(())
    }

    fn install_backend_batch(
        &mut self,
        back_alloc: (*mut u8, usize, Option<usize>),
    ) -> Result<NonNull<u8>> {
        let head = Self::non_null_chunk(back_alloc.0)?;
        let returned_count = back_alloc.1;
        if returned_count == 0 {
            return Err(AllocError::ESIZE);
        }

        if let Some(bump) = back_alloc.2 {
            let remaining = returned_count.checked_sub(1).ok_or(AllocError::ESIZE)?;
            if remaining == 0 {
                self.clear_bump_batch();
                return Ok(head);
            }
            if bump == 0 || remaining > i32::MAX as usize || bump > i32::MAX as usize {
                return Err(AllocError::ESIZE);
            }
            let max_offset = bump.checked_mul(remaining).ok_or(AllocError::ESIZE)?;
            (back_alloc.0 as usize)
                .checked_add(max_offset)
                .ok_or(AllocError::ESIZE)?;
            let bump_ptr = (back_alloc.0 as usize)
                .checked_add(bump)
                .ok_or(AllocError::ESIZE)?;
            self.bump_count = remaining as i32;
            self.bump_unit = bump as i32;
            self.bump_ptr = bump_ptr;
        } else {
            let remaining = returned_count.checked_sub(1).ok_or(AllocError::ESIZE)?;
            if remaining == 0 {
                return Ok(head);
            }
            self.install_linked_backend_tail(head, remaining)?;
        }

        Ok(head)
    }

    pub fn allocate(&mut self, idx: usize, align: usize) -> Result<NonNull<u8>> {
        let mut alignment_miss_streak = 0;
        self.allocate_with_alignment_miss_state(idx, align, &mut alignment_miss_streak)
    }

    fn allocate_with_alignment_miss_state(
        &mut self,
        idx: usize,
        align: usize,
        alignment_miss_streak: &mut u8,
    ) -> Result<NonNull<u8>> {
        if align == 0 || !align.is_power_of_two() || align > crate::PAGE_SIZE {
            return Err(AllocError::ESIZE);
        }

        let mut recorded_strict_alignment_miss = false;

        //case 1: we can reuse previous
        if self.list.length > 0 {
            let ans = self.list.pop_unchecked_aligned(align);
            if !ans.is_null() {
                Self::reset_alignment_miss_streak(alignment_miss_streak);
                return Self::non_null_chunk(ans);
            }
            if align > align_of::<usize>() && idx < TOTAL_SIZE_CLASS {
                recorded_strict_alignment_miss = true;
                self.trim_after_alignment_miss(
                    idx,
                    get_rounded_size_by_idx(idx),
                    alignment_miss_streak,
                );
            }
        }
        // case 2: consume the current contiguous bump batch.
        //
        // A strict-alignment request can miss every remaining slot in the batch
        // after the naturally aligned head was already handed out.  Those
        // skipped slots are still valid for ordinary allocations, so keep a hot
        // prefix locally, but do not let a high-alignment miss pin the whole
        // batch in this thread before asking the slab for another page.
        let mut ans = 0usize;
        let mut cached_misaligned_bump_slots = 0usize;
        while self.bump_count > 0 && ans == 0 {
            let bump_ptr = self.take_bump_slot()?;
            if bump_ptr & (align - 1) == 0 {
                ans = bump_ptr;
            } else {
                self.free(bump_ptr as *mut u8);
                cached_misaligned_bump_slots = cached_misaligned_bump_slots
                    .checked_add(1)
                    .ok_or(AllocError::ESIZE)?;
            }
        }
        if ans != 0 {
            Self::reset_alignment_miss_streak(alignment_miss_streak);
            Self::non_null_chunk(ans as *mut u8)
        } else {
            if cached_misaligned_bump_slots != 0
                && align > align_of::<usize>()
                && idx < TOTAL_SIZE_CLASS
            {
                if !recorded_strict_alignment_miss {
                    self.trim_after_alignment_miss(
                        idx,
                        get_rounded_size_by_idx(idx),
                        alignment_miss_streak,
                    );
                } else {
                    self.trim_after_recorded_alignment_miss(
                        idx,
                        get_rounded_size_by_idx(idx),
                        *alignment_miss_streak,
                    );
                }
            }
            //allocate from back
            let back_alloc = try_global_zone()?.allocate_batch_from_slab(idx, align)?;
            // Do not reset the miss streak here: a backend batch only proves
            // the slab can satisfy the request, not that retained local cached
            // objects are useful for this thread's allocation pattern.
            self.install_backend_batch(back_alloc)
        }
    }
}

#[repr(align(8))]
pub struct ThreadCache {
    list: [ThreadCacheUnit; TOTAL_SIZE_CLASS],
    alignment_miss_streaks: AlignmentMissStreaks,
    active_cached_class_bits: ActiveCachedClassBits,
    /// Approximate retained bytes in per-class local free objects.
    ///
    /// This is deliberately a single word rather than a per-class counter
    /// array: linked-list length and bump-batch length already live in each
    /// `ThreadCacheUnit`, so the aggregate only needs a cheap hot-path budget
    /// check.  Counting both representations matters because an idle thread can
    /// otherwise keep one slab bump tail per size class while the old aggregate
    /// only saw linked free-list nodes.
    cached_object_bytes: usize,
    /// Number of size classes that currently retain at least one local object.
    ///
    /// This mirrors `cached_object_bytes` at class granularity so the hot
    /// aggregate-budget check can choose the sparse/dense policy without
    /// scanning every size class on each allocation/deallocation.  It is a
    /// single word, not a per-class side table; repair paths recompute it from
    /// the existing list/bump metadata whenever ownership is uncertain.
    active_cached_classes: usize,
}

impl ThreadCache {
    pub const fn new() -> Self {
        Self {
            list: [ThreadCacheUnit::new(); TOTAL_SIZE_CLASS],
            alignment_miss_streaks: AlignmentMissStreaks::new(),
            active_cached_class_bits: ActiveCachedClassBits::new(),
            cached_object_bytes: 0,
            active_cached_classes: 0,
        }
    }
    pub fn init(&mut self) {}

    #[inline]
    fn class_cached_object_bytes(idx: usize, unit: &ThreadCacheUnit) -> usize {
        if idx == 0 || idx >= TOTAL_SIZE_CLASS {
            return 0;
        }
        unit.list
            .length
            .saturating_add(unit.bump_len())
            .saturating_mul(get_rounded_size_by_idx(idx))
    }

    fn recompute_cached_object_accounting(&self) -> (usize, usize, ActiveCachedClassBits) {
        let mut total = 0usize;
        let mut active_classes = 0usize;
        let mut active_bits = ActiveCachedClassBits::new();
        for idx in 1..self.list.len() {
            let class_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            total = total.saturating_add(class_bytes);
            if class_bytes != 0 {
                active_classes = active_classes.saturating_add(1);
                active_bits.set_present(idx, true);
            }
        }
        (total, active_classes, active_bits)
    }

    fn recompute_cached_object_state(&self) -> (usize, usize) {
        let (bytes, active_classes, _active_bits) = self.recompute_cached_object_accounting();
        (bytes, active_classes)
    }

    fn recompute_cached_object_bytes(&self) -> usize {
        self.recompute_cached_object_state().0
    }

    fn largest_retained_class_exact(&self) -> (usize, usize) {
        let mut best_idx = usize::MAX;
        let mut best_bytes = 0usize;

        for idx in 1..self.list.len() {
            let class_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            if class_bytes > best_bytes {
                best_idx = idx;
                best_bytes = class_bytes;
            }
        }

        (best_idx, best_bytes)
    }

    fn footprint_snapshot(&self) -> ThreadCacheFootprintSnapshot {
        let (exact_cached_object_bytes, exact_active_cached_classes, _active_bits) =
            self.recompute_cached_object_accounting();
        let (flush_bytes, target_bytes) =
            thread_cache_total_pressure_budget(exact_active_cached_classes);
        let hard_flush_bytes = thread_cache_total_blocking_flush_limit(exact_active_cached_classes);
        let (largest_retained_class_idx, largest_retained_class_bytes) =
            self.largest_retained_class_exact();

        ThreadCacheFootprintSnapshot {
            cache_initialized: true,
            cached_object_bytes: self.cached_object_bytes,
            exact_cached_object_bytes,
            active_cached_classes: self.active_cached_classes,
            exact_active_cached_classes,
            largest_retained_class_idx,
            largest_retained_class_bytes,
            flush_bytes,
            target_bytes,
            hard_flush_bytes,
            accounting_matches_exact: self.cached_object_bytes == exact_cached_object_bytes
                && self.active_cached_classes == exact_active_cached_classes,
            over_soft_budget: exact_cached_object_bytes > flush_bytes,
            over_target_budget: exact_cached_object_bytes > target_bytes,
        }
    }

    #[inline]
    fn sync_cached_object_bytes(&mut self) {
        let (bytes, active_classes, active_bits) = self.recompute_cached_object_accounting();
        self.cached_object_bytes = bytes;
        self.active_cached_classes = active_classes;
        self.active_cached_class_bits = active_bits;
    }

    #[inline]
    fn account_class_cached_object_change(
        &mut self,
        idx: usize,
        before_bytes: usize,
        after_bytes: usize,
    ) {
        if after_bytes >= before_bytes {
            self.cached_object_bytes = self
                .cached_object_bytes
                .saturating_add(after_bytes - before_bytes);
        } else {
            self.cached_object_bytes = self
                .cached_object_bytes
                .saturating_sub(before_bytes - after_bytes);
        }

        match (before_bytes == 0, after_bytes == 0) {
            (true, false) => {
                if self.active_cached_class_bits.set_present(idx, true) {
                    self.active_cached_classes = self.active_cached_classes.saturating_add(1);
                }
            }
            (false, true) => {
                if self.active_cached_class_bits.set_present(idx, false) {
                    self.active_cached_classes = self.active_cached_classes.saturating_sub(1);
                }
            }
            _ => {}
        }
    }

    fn account_class_cached_object_change_after_flush(
        &mut self,
        idx: usize,
        before_bytes: usize,
        after_bytes: usize,
        disposition: FlushDisposition,
    ) {
        if matches!(disposition, FlushDisposition::FailedAfterTransferAttempt) {
            self.sync_cached_object_bytes();
        } else {
            self.account_class_cached_object_change(idx, before_bytes, after_bytes);
        }
    }

    fn trim_class_to_keep_length_result(
        &mut self,
        idx: usize,
        keep_length: usize,
        blocking_flush: bool,
    ) -> TrimClassResult {
        let before_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
        let disposition = self.list[idx].flush_suffix_to_slab(idx, keep_length, blocking_flush);
        let after_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
        self.account_class_cached_object_change_after_flush(
            idx,
            before_bytes,
            after_bytes,
            disposition,
        );
        TrimClassResult {
            reduced_cached_bytes: before_bytes > after_bytes,
            busy_no_transfer: disposition == FlushDisposition::BusyNoTransfer,
        }
    }

    fn trim_class_to_keep_length(
        &mut self,
        idx: usize,
        keep_length: usize,
        blocking_flush: bool,
    ) -> bool {
        self.trim_class_to_keep_length_result(idx, keep_length, blocking_flush)
            .reduced_cached_bytes
    }

    fn trim_class_for_total_budget_result(
        &mut self,
        idx: usize,
        blocking_flush: bool,
    ) -> TrimClassResult {
        if idx == 0 || idx >= self.list.len() {
            return TrimClassResult::default();
        }

        let rounded_size = get_rounded_size_by_idx(idx);
        if rounded_size == 0 {
            return TrimClassResult::default();
        }

        if self.list[idx].bump_len() != 0 {
            let before_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            self.list[idx].drain_bump_batch_to_list();
            let after_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            self.account_class_cached_object_change(idx, before_bytes, after_bytes);
        }

        let length = self.list[idx].list.length;
        if length <= 1 {
            return TrimClassResult::default();
        }
        let keep_length = thread_cache_target_keep_length(length, rounded_size);
        if keep_length >= length {
            return TrimClassResult::default();
        }

        self.trim_class_to_keep_length_result(idx, keep_length, blocking_flush)
    }

    fn trim_class_for_aggregate_pressure_result(
        &mut self,
        idx: usize,
        target_total: usize,
        blocking_flush: bool,
    ) -> TrimClassResult {
        if idx == 0 || idx >= self.list.len() {
            return TrimClassResult::default();
        }

        let rounded_size = get_rounded_size_by_idx(idx);
        if rounded_size == 0 {
            return TrimClassResult::default();
        }

        if self.list[idx].bump_len() != 0 {
            let before_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            self.list[idx].drain_bump_batch_to_list();
            let after_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            self.account_class_cached_object_change(idx, before_bytes, after_bytes);
        }

        let length = self.list[idx].list.length;
        if length == 0 {
            return TrimClassResult::default();
        }

        let keep_length = thread_cache_aggregate_pressure_keep_length(
            self.cached_object_bytes,
            target_total,
            length,
            rounded_size,
        );
        if keep_length >= length {
            return TrimClassResult::default();
        }

        self.trim_class_to_keep_length_result(idx, keep_length, blocking_flush)
    }

    #[inline]
    fn active_cached_class_count(&self) -> usize {
        self.active_cached_classes
    }

    #[inline]
    fn previous_active_cached_class_idx(&self, start_idx: usize) -> Option<usize> {
        self.active_cached_class_bits
            .previous_at_or_below(start_idx)
    }

    /// Return the active size class that currently retains the most local bytes.
    ///
    /// The active-class bitmap is already maintained for aggregate trimming, so
    /// this pressure-only scan adds no persistent metadata.  It improves trim
    /// decisions for workloads where a lower size class keeps many objects
    /// while a higher class keeps only a tiny hot prefix: trimming by retained
    /// bytes returns the likely offender before an index-ordered sweep would
    /// spend work on unrelated classes.
    fn largest_retained_active_class_idx_excluding(
        &self,
        excluded_classes: &ActiveCachedClassBits,
    ) -> Option<usize> {
        let mut best_idx = None;
        let mut best_bytes = 0usize;
        let mut next_idx = self.previous_active_cached_class_idx(self.list.len().saturating_sub(1));

        while let Some(idx) = next_idx {
            if !excluded_classes.is_present(idx) {
                let class_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
                if class_bytes > best_bytes {
                    best_idx = Some(idx);
                    best_bytes = class_bytes;
                }
            }
            next_idx = idx
                .checked_sub(1)
                .and_then(|prev| self.previous_active_cached_class_idx(prev));
        }

        best_idx
    }

    fn largest_retained_active_class_idx(&self) -> Option<usize> {
        self.largest_retained_active_class_idx_excluding(&ActiveCachedClassBits::new())
    }

    /// Enforce the aggregate per-thread free-list budget.
    ///
    /// Per-class flushing is still the first line of defense and is O(1) for a
    /// deallocation.  This second-tier budget only runs after the maintained
    /// aggregate crosses the selected soft cap: the normal 2MiB sparse cap, or
    /// the tighter dense cap when many size classes retain private cold
    /// objects.  The caller may name the class that just
    /// changed; trying that transient offender first avoids adding any
    /// persistent per-thread array/ring/bitset while reducing bursty private
    /// retained-object and fragmentation pressure where a lower class just grew
    /// but an unrelated larger class would otherwise be flushed first.
    ///
    /// The first phase preserves each class's normal hot target for locality.
    /// If the aggregate target is still not restored, a second aggregate-
    /// pressure phase may trim below those per-class hot targets.  Both phases
    /// pick the currently largest retained-byte class first, not the highest
    /// size-class index.  That keeps trim work focused on the actual private
    /// footprint offender and avoids turning a small high-index hot prefix into
    /// unnecessary churn merely because it sorts later in the class table.
    /// Trimming below the normal hot target intentionally sacrifices some local
    /// reuse only to bound idle-thread private retained objects; it is not a
    /// claim that RSS is immediately reclaimed.
    fn trim_total_cached_object_bytes(&mut self, preferred_idx: Option<usize>) {
        let active_cached_classes = self.active_cached_class_count();
        let (flush_bytes, target_bytes) = thread_cache_total_pressure_budget(active_cached_classes);
        if self.cached_object_bytes <= flush_bytes {
            return;
        }

        let hard_limit = thread_cache_total_blocking_flush_limit(active_cached_classes);
        let blocking_flush = self.cached_object_bytes > hard_limit;
        let mut busy_classes = ActiveCachedClassBits::new();
        let mut total_budget_tried_classes = ActiveCachedClassBits::new();

        if let Some(idx) = preferred_idx {
            let result = self.trim_class_for_total_budget_result(idx, blocking_flush);
            total_budget_tried_classes.set_present(idx, true);
            if result.busy_no_transfer {
                busy_classes.set_present(idx, true);
            }
            if self.cached_object_bytes <= target_bytes {
                return;
            }
        }

        while self.cached_object_bytes > target_bytes {
            let idx = match self
                .largest_retained_active_class_idx_excluding(&total_budget_tried_classes)
            {
                Some(idx) => idx,
                None => break,
            };
            let result = self.trim_class_for_total_budget_result(idx, blocking_flush);
            total_budget_tried_classes.set_present(idx, true);
            if result.busy_no_transfer {
                busy_classes.set_present(idx, true);
            }
            if self.cached_object_bytes <= target_bytes {
                return;
            }
        }

        let mut aggregate_tried_classes = busy_classes;
        while self.cached_object_bytes > target_bytes {
            let idx =
                match self.largest_retained_active_class_idx_excluding(&aggregate_tried_classes) {
                    Some(idx) => idx,
                    None => break,
                };
            let _ =
                self.trim_class_for_aggregate_pressure_result(idx, target_bytes, blocking_flush);
            aggregate_tried_classes.set_present(idx, true);
        }
    }

    fn cleanup_active_cached_classes(&mut self) {
        let mut next_idx = self.previous_active_cached_class_idx(self.list.len().saturating_sub(1));

        while let Some(idx) = next_idx {
            next_idx = idx
                .checked_sub(1)
                .and_then(|prev| self.previous_active_cached_class_idx(prev));
            self.list[idx].clean_up(idx);
        }
    }

    pub fn cleanup_cache_unchecked(&mut self) {
        // Fast path: normal allocation/deallocation maintains the active-class
        // bitmap, so thread teardown only visits classes that actually retain
        // local objects.  The bitmap is still just a hint: tests and defensive
        // error paths can mutate units directly, so the post-clean accounting
        // sync below can discover stale hidden objects and force one repair pass.
        self.cleanup_active_cached_classes();
        self.alignment_miss_streaks = AlignmentMissStreaks::new();
        self.sync_cached_object_bytes();

        if self.cached_object_bytes != 0 {
            self.cleanup_active_cached_classes();
            self.sync_cached_object_bytes();
        }
    }

    pub fn allocate(&mut self, layout: Layout) -> Result<NonNull<u8>> {
        if layout.size() != 0 && layout.align() > crate::PAGE_SIZE {
            return Err(AllocError::ESIZE);
        }

        // 1. round the size up to next size class
        let cls = get_size_class(layout.size());

        // 2. try to pop one from freelist
        if let SizeClass::Base(idx) = cls {
            if unlikely(idx == 0) {
                return NonNull::new(layout.align() as *mut u8).ok_or(AllocError::ENOMEM);
            }
            let before_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            let mut alignment_miss_streak = self.alignment_miss_streaks.get(idx);
            let result = {
                let size_cache: &mut ThreadCacheUnit = &mut self.list[idx];
                size_cache.allocate_with_alignment_miss_state(
                    idx,
                    layout.align(),
                    &mut alignment_miss_streak,
                )
            };
            self.alignment_miss_streaks.set(idx, alignment_miss_streak);
            let after_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            self.account_class_cached_object_change(idx, before_bytes, after_bytes);
            self.trim_total_cached_object_bytes(Some(idx));
            result
        } else {
            // 3. Large objects bypass the thread cache.  They are already page
            // backed and keeping them per-thread would amplify external
            // fragmentation and private RSS after producer/consumer handoff.
            try_global_zone()?.allocate_large(layout)
        }
    }

    pub fn deallocate(&mut self, ptr: NonNull<u8>, layout: Layout) {
        // 1. round the size up to next size class
        let cls = get_size_class(layout.size());

        // 2. try to push the ptr to freelist
        if let SizeClass::Base(idx) = cls {
            if unlikely(idx == 0) {
                return;
            }
            let rounded_size = get_rounded_size_by_idx(idx);
            let before_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            let disposition = {
                let size_cache: &mut ThreadCacheUnit = &mut self.list[idx];
                if thread_cache_should_bypass_local_cache(idx, rounded_size) {
                    // Single-slot spans have no useful batching value in a
                    // thread-local cache: one cached object pins an entire
                    // empty slab/span that the slab layer is otherwise able to
                    // recycle immediately.  Return it through the same batch
                    // handoff path and only keep it locally if the zone is not
                    // reachable before ownership transfer starts.
                    Some(size_cache.deallocate_direct_to_slab(idx, ptr))
                } else {
                    size_cache.deallocate(idx, ptr, rounded_size)
                }
            };
            let after_bytes = Self::class_cached_object_bytes(idx, &self.list[idx]);
            if let Some(disposition) = disposition {
                self.account_class_cached_object_change_after_flush(
                    idx,
                    before_bytes,
                    after_bytes,
                    disposition,
                );
            } else {
                self.account_class_cached_object_change(idx, before_bytes, after_bytes);
            }
            self.trim_total_cached_object_bytes(Some(idx));
        } else {
            // 3. Large chunks go straight back to the zone for immediate reuse
            // by any thread instead of being pinned in a local cache.
            if let Ok(zone) = try_global_zone() {
                zone.deallocate_large(ptr, layout);
            }
        }
    }
}

use super::*;
#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sync::general_thread_local::{load_tls, register_tls_key, save_tls};
use alloc_macros::tls_static;
#[cfg(not(feature = "fixed_heap"))]
unsafe fn release_thread_cache_storage(ptr: *mut ThreadCache) {
    let tcache = match ptr.as_mut() {
        Some(tcache) => tcache,
        None => return,
    };
    tcache.cleanup_cache_unchecked();
    META_BUMP
        .lock()
        .dealloc(ptr as *mut usize, core::mem::size_of::<ThreadCache>());
}

#[cfg(all(not(feature = "fixed_heap"), not(windows)))]
unsafe extern "C" fn free_thread_cache(ptr: *mut libc::c_void) {
    let ptr = ptr as *mut ThreadCache;
    if !ptr.is_null() {
        // Semantic caches/quarantine own allocator objects that must be
        // returned through this still-live thread cache. The drain is
        // allocation-free and runs on the owning thread.
        let allocator = crate::cache::RustAllocator::new();
        let _ = crate::alloc_api::type_isolation::drain_current_thread_semantic_state(&allocator);
    }
    // Clear every TLS view before touching the cache. Other TLS destructors may
    // allocate after this callback returns (or even re-enter while cleanup is
    // in progress); leaving the generated Rust TLS slot populated would expose
    // metadata storage after it has been returned to META_BUMP.
    if globaltcache_store_tls_value(core::ptr::null_mut()).is_err() {
        // An authoritative pthread/FLS slot that still points at storage about
        // to be recycled would become a use-after-free on the next allocator
        // access. Fail closed instead of returning that storage to META_BUMP.
        globaltcache_tls_storage_failure();
    }
    release_thread_cache_storage(ptr);
}

#[cfg(all(not(feature = "fixed_heap"), windows))]
unsafe extern "C" fn free_thread_cache(ptr: *mut libc::c_void) {
    let ptr = ptr.cast::<ThreadCache>();
    if ptr.is_null() {
        return;
    }

    // FlsSetValue always targets the calling/current fiber. Windows can invoke
    // this callback for DeleteFiber(B) while fiber A remains current, so the
    // callback argument is the only ownership identity that is safe to use.
    // In particular, clearing GlobalTcache here would clear A rather than B.
    // Rust semantic retained caches are OS-thread-local rather than fiber-local,
    // however, so a non-current fiber callback must flush those allocator-owned
    // objects before its last usable ThreadCache is reclaimed. Live memory-tag,
    // recovery, scope, and compiler-cursor state still belongs to the thread
    // and is cleared only by the current-owner thread-exit callback.
    let current = globaltcache_load_tls_value();
    let allocator = crate::cache::RustAllocator::new();
    if current == ptr {
        let released_count =
            crate::alloc_api::type_isolation::drain_current_thread_semantic_state(&allocator);
        #[cfg(test)]
        {
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.fetch_add(1, Ordering::Relaxed);
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_RELEASED.fetch_add(released_count, Ordering::Relaxed);
        }
        #[cfg(not(test))]
        let _ = released_count;
    } else {
        // When A has no cache and DeleteFiber(B) invokes B's callback, bind B
        // just long enough for allocation-free raw deallocation. If either FLS
        // update fails, retain B's storage: leaking is safer than reclaiming a
        // cache that the current FLS slot may still expose.
        let temporarily_bound = current.is_null();
        if temporarily_bound && globaltcache_store_tls_value(ptr).is_err() {
            return;
        }

        let released_count =
            crate::alloc_api::type_isolation::drain_current_thread_semantic_retained_state(
                &allocator,
            );

        if temporarily_bound && globaltcache_store_tls_value(core::ptr::null_mut()).is_err() {
            return;
        }

        #[cfg(test)]
        {
            WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.fetch_add(1, Ordering::Relaxed);
            WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED.fetch_add(released_count, Ordering::Relaxed);
        }
        #[cfg(not(test))]
        let _ = released_count;
    }
    release_thread_cache_storage(ptr);
}
#[cfg(not(feature = "fixed_heap"))]
tls_static! {
    ThreadCache GlobalTcache, free_thread_cache
}

#[cfg(all(test, windows, not(feature = "fixed_heap")))]
static WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS: AtomicUsize = AtomicUsize::new(0);
#[cfg(all(test, windows, not(feature = "fixed_heap")))]
static WINDOWS_CURRENT_OWNER_DESTRUCTOR_RELEASED: AtomicUsize = AtomicUsize::new(0);
#[cfg(all(test, windows, not(feature = "fixed_heap")))]
static WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS: AtomicUsize = AtomicUsize::new(0);
#[cfg(all(test, windows, not(feature = "fixed_heap")))]
static WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED: AtomicUsize = AtomicUsize::new(0);

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    static WINDOWS_MAIN_FIBER: core::sync::atomic::AtomicUsize =
        core::sync::atomic::AtomicUsize::new(0);
    #[cfg(all(windows, not(feature = "fixed_heap")))]
    static WINDOWS_FIBER_B_INITIAL_TCACHE: core::sync::atomic::AtomicUsize =
        core::sync::atomic::AtomicUsize::new(usize::MAX);
    #[cfg(all(windows, not(feature = "fixed_heap")))]
    static WINDOWS_FIBER_B_TCACHE: core::sync::atomic::AtomicUsize =
        core::sync::atomic::AtomicUsize::new(usize::MAX);

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    unsafe extern "system" fn global_tcache_fiber_b_entry(_parameter: *mut winapi::ctypes::c_void) {
        WINDOWS_FIBER_B_INITIAL_TCACHE.store(
            globaltcache_load_tls_value() as usize,
            core::sync::atomic::Ordering::Release,
        );
        let _ = (&*GlobalTcache).footprint_snapshot();
        WINDOWS_FIBER_B_TCACHE.store(
            globaltcache_load_tls_value() as usize,
            core::sync::atomic::Ordering::Release,
        );

        loop {
            winapi::um::winbase::SwitchToFiber(
                WINDOWS_MAIN_FIBER.load(core::sync::atomic::Ordering::Acquire)
                    as *mut winapi::ctypes::c_void,
            );
        }
    }

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    unsafe extern "system" fn global_tcache_fiber_b_semantic_entry(
        _parameter: *mut winapi::ctypes::c_void,
    ) {
        WINDOWS_FIBER_B_INITIAL_TCACHE.store(
            globaltcache_load_tls_value() as usize,
            core::sync::atomic::Ordering::Release,
        );

        let allocator = crate::cache::RustAllocator::new();
        let layout =
            Layout::from_size_align(64, align_of::<usize>()).expect("valid semantic cache layout");
        let metadata = crate::alloc_api::type_isolation::AllocationMetadata::for_type(0xC003_B0B0)
            .with_module(0xC003)
            .with_callsite(0xA110_B0B0)
            .with_flags(crate::alloc_api::type_isolation::FLAG_TYPE_ISOLATED);
        let ptr = allocator.alloc_with_metadata(layout, metadata);
        assert!(!ptr.is_null());
        allocator.dealloc_with_metadata(ptr, layout, metadata);

        WINDOWS_FIBER_B_TCACHE.store(
            globaltcache_load_tls_value() as usize,
            core::sync::atomic::Ordering::Release,
        );
        loop {
            winapi::um::winbase::SwitchToFiber(
                WINDOWS_MAIN_FIBER.load(core::sync::atomic::Ordering::Acquire)
                    as *mut winapi::ctypes::c_void,
            );
        }
    }

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    #[test]
    #[ignore = "uses the production GlobalTcache FLS slot; run in an isolated Windows process"]
    fn global_thread_cache_values_are_fiber_local_on_windows() {
        unsafe {
            WINDOWS_FIBER_B_INITIAL_TCACHE.store(usize::MAX, core::sync::atomic::Ordering::Relaxed);
            WINDOWS_FIBER_B_TCACHE.store(usize::MAX, core::sync::atomic::Ordering::Relaxed);
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.store(0, Ordering::Relaxed);
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_RELEASED.store(0, Ordering::Relaxed);
            WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.store(0, Ordering::Relaxed);
            WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED.store(0, Ordering::Relaxed);

            let main_fiber = winapi::um::winbase::ConvertThreadToFiber(core::ptr::null_mut());
            assert!(!main_fiber.is_null(), "ConvertThreadToFiber failed");
            WINDOWS_MAIN_FIBER.store(main_fiber as usize, core::sync::atomic::Ordering::Release);

            let fiber_a_initial = globaltcache_load_tls_value() as usize;
            let _ = (&*GlobalTcache).footprint_snapshot();
            let fiber_a_tcache = globaltcache_load_tls_value() as usize;
            let allocator = crate::cache::RustAllocator::new();
            let semantic_layout = Layout::from_size_align(64, align_of::<usize>())
                .expect("valid semantic cache layout");
            let semantic_metadata =
                crate::alloc_api::type_isolation::AllocationMetadata::for_type(0xC003_F1B3)
                    .with_module(0xC003)
                    .with_callsite(0xA110_F1B3)
                    .with_flags(crate::alloc_api::type_isolation::FLAG_TYPE_ISOLATED);
            let semantic_ptr = allocator.alloc_with_metadata(semantic_layout, semantic_metadata);
            assert!(!semantic_ptr.is_null());
            allocator.dealloc_with_metadata(semantic_ptr, semantic_layout, semantic_metadata);
            let semantic_before_delete =
                crate::alloc_api::type_isolation::type_isolation_side_cache_snapshot();
            assert_eq!(semantic_before_delete.occupied_entries, 1);

            let fiber_b = winapi::um::winbase::CreateFiber(
                0,
                Some(global_tcache_fiber_b_entry),
                core::ptr::null_mut(),
            );
            if fiber_b.is_null() {
                let _ = crate::alloc_api::type_isolation::drain_current_thread_semantic_state(
                    &allocator,
                );
                assert!(
                    globaltcache_store_tls_value(core::ptr::null_mut()).is_ok(),
                    "failed to clear fiber A's cache after CreateFiber failure"
                );
                free_thread_cache(fiber_a_tcache as *mut libc::c_void);
                let _ = winapi::um::winbase::ConvertFiberToThread();
                panic!("CreateFiber failed");
            }

            winapi::um::winbase::SwitchToFiber(fiber_b);
            let fiber_b_initial =
                WINDOWS_FIBER_B_INITIAL_TCACHE.load(core::sync::atomic::Ordering::Acquire);
            let fiber_b_tcache = WINDOWS_FIBER_B_TCACHE.load(core::sync::atomic::Ordering::Acquire);
            let fiber_a_after_switch = globaltcache_load_tls_value() as usize;

            winapi::um::winbase::DeleteFiber(fiber_b);
            let fiber_a_after_delete = globaltcache_load_tls_value() as usize;

            assert_eq!(fiber_a_initial, 0, "fiber A started with a stale cache");
            assert_ne!(fiber_a_tcache, 0, "fiber A did not create a cache");
            assert_eq!(
                fiber_b_initial, 0,
                "fiber B inherited fiber A's production thread cache"
            );
            assert_ne!(fiber_b_tcache, 0, "fiber B did not create a cache");
            assert_ne!(
                fiber_b_tcache, fiber_a_tcache,
                "two fibers on one thread shared a production thread cache"
            );
            assert_eq!(fiber_a_after_switch, fiber_a_tcache);
            assert_eq!(
                fiber_a_after_delete, fiber_a_tcache,
                "deleting fiber B disturbed fiber A's production cache"
            );
            let semantic_after_delete =
                crate::alloc_api::type_isolation::type_isolation_side_cache_snapshot();
            assert_eq!(
                semantic_after_delete.occupied_entries, 0,
                "deleting fiber B left allocator-owned retained state without a guaranteed future callback owner"
            );
            assert_eq!(semantic_after_delete.retained_bytes, 0);
            assert_eq!(
                WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
                0,
                "fiber B's callback was mistaken for current-owner teardown"
            );
            assert_eq!(
                WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
                1,
                "fiber B's callback did not flush shared retained semantic state"
            );
            assert_eq!(
                WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED.load(Ordering::Relaxed),
                semantic_before_delete.occupied_entries,
                "fiber B's callback did not release the retained semantic object"
            );

            // The FLS destructor deliberately does not mutate the current
            // fiber's slot. Explicitly detach A before directly reclaiming its
            // storage in this isolated test.
            assert_eq!(
                crate::alloc_api::type_isolation::drain_current_thread_semantic_state(&allocator),
                0,
                "non-current callback left allocator-owned retained state for final cleanup"
            );
            assert!(
                globaltcache_store_tls_value(core::ptr::null_mut()).is_ok(),
                "failed to detach fiber A's cache before test cleanup"
            );
            free_thread_cache(fiber_a_tcache as *mut libc::c_void);
            let fiber_a_after_cleanup = globaltcache_load_tls_value() as usize;
            let converted_to_thread = winapi::um::winbase::ConvertFiberToThread();

            assert_eq!(fiber_a_after_cleanup, 0);
            assert_ne!(converted_to_thread, 0, "ConvertFiberToThread failed");
        }
    }

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    #[test]
    #[ignore = "uses the process-wide production GlobalTcache FLS key; run in an isolated Windows process"]
    fn windows_delete_fiber_drains_shared_semantic_cache_when_current_fiber_has_no_tcache() {
        WINDOWS_FIBER_B_INITIAL_TCACHE.store(usize::MAX, Ordering::Relaxed);
        WINDOWS_FIBER_B_TCACHE.store(usize::MAX, Ordering::Relaxed);
        WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.store(0, Ordering::Relaxed);
        WINDOWS_CURRENT_OWNER_DESTRUCTOR_RELEASED.store(0, Ordering::Relaxed);
        WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.store(0, Ordering::Relaxed);
        WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED.store(0, Ordering::Relaxed);

        std::thread::spawn(|| unsafe {
            let main_fiber = winapi::um::winbase::ConvertThreadToFiber(core::ptr::null_mut());
            assert!(!main_fiber.is_null(), "ConvertThreadToFiber failed");
            WINDOWS_MAIN_FIBER.store(main_fiber as usize, Ordering::Release);
            assert!(
                globaltcache_load_tls_value().is_null(),
                "fiber A unexpectedly owned a ThreadCache before fiber B ran"
            );

            let fiber_b = winapi::um::winbase::CreateFiber(
                0,
                Some(global_tcache_fiber_b_semantic_entry),
                core::ptr::null_mut(),
            );
            if fiber_b.is_null() {
                let _ = winapi::um::winbase::ConvertFiberToThread();
                panic!("CreateFiber failed");
            }

            winapi::um::winbase::SwitchToFiber(fiber_b);
            assert_eq!(
                WINDOWS_FIBER_B_INITIAL_TCACHE.load(Ordering::Acquire),
                0,
                "fiber B inherited a stale ThreadCache"
            );
            assert_ne!(
                WINDOWS_FIBER_B_TCACHE.load(Ordering::Acquire),
                0,
                "fiber B did not create a ThreadCache"
            );
            assert!(
                globaltcache_load_tls_value().is_null(),
                "fiber A acquired fiber B's ThreadCache after switching back"
            );
            let retained = crate::alloc_api::type_isolation::type_isolation_side_cache_snapshot();
            assert_eq!(retained.occupied_entries, 1);
            assert!(retained.retained_bytes >= 64);

            winapi::um::winbase::DeleteFiber(fiber_b);

            assert!(
                globaltcache_load_tls_value().is_null(),
                "temporary callback binding escaped into fiber A"
            );
            let after = crate::alloc_api::type_isolation::type_isolation_side_cache_snapshot();
            assert_eq!(after.occupied_entries, 0);
            assert_eq!(after.retained_bytes, 0);
            assert_eq!(
                WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
                0,
                "non-current fiber deletion ran full thread-exit teardown"
            );
            assert_eq!(
                WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
                1,
                "non-current fiber deletion skipped retained-state teardown"
            );
            assert_eq!(
                WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED.load(Ordering::Relaxed),
                1,
                "non-current fiber deletion leaked the retained semantic object"
            );
            assert_ne!(
                winapi::um::winbase::ConvertFiberToThread(),
                0,
                "ConvertFiberToThread failed"
            );
        })
        .join()
        .expect("A-null/B-populated FLS regression worker should exit safely");

        assert_eq!(
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
            0,
            "thread exit unexpectedly found a current-fiber cache after temporary binding cleanup"
        );
    }

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    #[test]
    #[ignore = "uses the process-wide production GlobalTcache FLS key; run in an isolated Windows process"]
    fn windows_current_owner_thread_exit_drains_semantic_state_once() {
        WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.store(0, Ordering::Relaxed);
        WINDOWS_CURRENT_OWNER_DESTRUCTOR_RELEASED.store(0, Ordering::Relaxed);
        WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.store(0, Ordering::Relaxed);
        WINDOWS_NON_CURRENT_DESTRUCTOR_RELEASED.store(0, Ordering::Relaxed);

        std::thread::spawn(|| unsafe {
            let allocator = crate::cache::RustAllocator::new();
            let layout = Layout::from_size_align(64, align_of::<usize>())
                .expect("valid semantic cache layout");
            let metadata =
                crate::alloc_api::type_isolation::AllocationMetadata::for_type(0xC003_7EAD)
                    .with_module(0xC003)
                    .with_callsite(0xA110_7EAD)
                    .with_flags(crate::alloc_api::type_isolation::FLAG_TYPE_ISOLATED);

            let ptr = allocator.alloc_with_metadata(layout, metadata);
            assert!(!ptr.is_null());
            allocator.dealloc_with_metadata(ptr, layout, metadata);
            let retained = crate::alloc_api::type_isolation::type_isolation_side_cache_snapshot();
            assert_eq!(retained.occupied_entries, 1);
            assert!(retained.retained_bytes >= layout.size());
            assert!(
                !globaltcache_load_tls_value().is_null(),
                "semantic allocation did not initialize the worker's production FLS cache"
            );
        })
        .join()
        .expect("Windows semantic teardown worker should finish");

        assert_eq!(
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
            1,
            "ordinary Windows thread exit must run current-owner semantic drain exactly once"
        );
        assert_eq!(
            WINDOWS_CURRENT_OWNER_DESTRUCTOR_RELEASED.load(Ordering::Relaxed),
            1,
            "ordinary Windows thread exit leaked the retained semantic cache object"
        );
        assert_eq!(
            WINDOWS_NON_CURRENT_DESTRUCTOR_DRAINS.load(Ordering::Relaxed),
            0,
            "ordinary Windows thread exit was mistaken for non-current fiber deletion"
        );
    }

    #[cfg(all(windows, not(feature = "fixed_heap")))]
    #[test]
    #[ignore = "mutates the process-wide production GlobalTcache FLS key; run in an isolated Windows process"]
    fn global_thread_cache_fls_failures_do_not_lose_cache_ownership_on_windows() {
        unsafe {
            use crate::pal::sync::general_thread_local::{
                fail_next_tls_registration_for_test, fail_next_tls_save_for_test, load_tls,
                tls_key_ready,
            };

            fail_next_tls_registration_for_test();
            assert!(
                !globaltcache_ensure_tsd_initialized(),
                "injected FlsAlloc failure was not observed"
            );
            assert!(!tls_key_ready());
            assert!(load_tls().is_null());
            assert!(
                globaltcache_reset_failed_tsd_initialization_for_test(),
                "failed initialization state was not reset for isolated retry"
            );
            assert!(
                globaltcache_ensure_tsd_initialized(),
                "FLS key retry failed"
            );

            let layout = Layout::new::<ThreadCache>();
            let first_allocation = MetadataAllocator {}
                .allocate(layout)
                .expect("metadata allocation should succeed");
            let first = first_allocation.as_non_null_ptr().as_ptr() as *mut ThreadCache;
            core::ptr::write(first, ThreadCache::new());

            fail_next_tls_save_for_test();
            let unpublished = globaltcache_store_tls_value(first)
                .expect_err("injected FlsSetValue failure was not observed");
            assert_eq!(unpublished, first, "failed store lost pointer ownership");
            assert!(
                globaltcache_load_tls_value().is_null(),
                "failed FlsSetValue unexpectedly published the cache"
            );
            globaltcache_reclaim_unpublished_tls_value(unpublished);

            let second_allocation = MetadataAllocator {}
                .allocate(layout)
                .expect("reclaimed metadata allocation should be reusable");
            let second = second_allocation.as_non_null_ptr().as_ptr() as *mut ThreadCache;
            assert_eq!(
                second, first,
                "failed FLS publication leaked the unpublished ThreadCache allocation"
            );
            core::ptr::write(second, ThreadCache::new());
            globaltcache_reclaim_unpublished_tls_value(second);
        }
    }

    #[cfg(all(not(windows), not(feature = "fixed_heap")))]
    #[test]
    fn unix_tls_save_failure_does_not_publish_unowned_cache() {
        std::thread::spawn(|| unsafe {
            use crate::pal::sync::general_thread_local::{
                fail_next_tls_save_for_test, tls_save_failure_count,
            };

            assert!(globaltcache_load_tls_value().is_null());
            let failures_before = tls_save_failure_count();
            let layout = Layout::new::<ThreadCache>();
            let allocation = MetadataAllocator {}
                .allocate(layout)
                .expect("metadata allocation should succeed");
            let cache = allocation.as_non_null_ptr().as_ptr() as *mut ThreadCache;
            core::ptr::write(cache, ThreadCache::new());

            fail_next_tls_save_for_test();
            let unpublished = globaltcache_store_tls_value(cache)
                .expect_err("injected pthread_setspecific failure was not observed");
            assert_eq!(unpublished, cache);
            assert!(
                globaltcache_load_tls_value().is_null(),
                "failed pthread publication exposed a cache without destructor ownership"
            );
            assert_eq!(tls_save_failure_count(), failures_before + 1);
            globaltcache_reclaim_unpublished_tls_value(unpublished);
        })
        .join()
        .expect("Unix TLS publication-failure worker should finish");
    }

    #[cfg(all(not(windows), not(feature = "fixed_heap")))]
    #[test]
    fn free_thread_cache_clears_tls_before_storage_reuse() {
        std::thread::spawn(|| unsafe {
            let _ = (&*GlobalTcache).footprint_snapshot();
            let first = globaltcache_load_tls_value();
            assert!(!first.is_null());

            free_thread_cache(first.cast::<libc::c_void>());
            assert!(globaltcache_load_tls_value().is_null());

            let allocator = crate::cache::RustAllocator::new();
            let layout = Layout::from_size_align(64, 16).expect("valid test layout");
            let ptr = allocator.alloc_raw(layout);
            assert!(!ptr.is_null());
            assert!(!globaltcache_load_tls_value().is_null());
            allocator.dealloc_raw(ptr, layout);
        })
        .join()
        .expect("TLS destructor regression worker should finish");
    }

    fn write_test_link_word(slot: *mut usize, next: usize) {
        // The allocator under test reads these words through raw list pointers.
        // Use raw writes so latest rustc does not treat the fixture setup as an
        // unused ordinary assignment before the raw-pointer consumer runs.
        unsafe {
            slot.write(next);
        }
    }

    #[cfg(feature = "fixed_heap")]
    fn fixed_heap_thread_cache_test_guard() -> spin::MutexGuard<'static, ()> {
        crate::sc::fixed_heap_test_guard()
    }

    #[cfg(feature = "fixed_heap")]
    struct FixedHeapZoneUnavailableGuard(*mut crate::zone::ZoneAllocator);

    #[cfg(feature = "fixed_heap")]
    impl FixedHeapZoneUnavailableGuard {
        fn install() -> Self {
            let saved = crate::zone::GLOBAL_ZONE_PTR
                .swap(core::ptr::null_mut(), core::sync::atomic::Ordering::AcqRel);
            Self(saved)
        }
    }

    #[cfg(feature = "fixed_heap")]
    impl Drop for FixedHeapZoneUnavailableGuard {
        fn drop(&mut self) {
            crate::zone::GLOBAL_ZONE_PTR.store(self.0, core::sync::atomic::Ordering::Release);
        }
    }

    #[cfg(feature = "fixed_heap")]
    fn real_subpage_large_size_class() -> (usize, usize) {
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            if rounded_size >= 1024 && rounded_size < crate::PAGE_SIZE {
                return (idx, rounded_size);
            }
        }
        panic!("test needs a sub-page size class of at least 1024 bytes");
    }

    #[cfg(feature = "fixed_heap")]
    fn real_subpage_size_class_larger_than(byte_budget: usize) -> Option<(usize, usize)> {
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            if rounded_size > byte_budget && rounded_size < crate::PAGE_SIZE {
                return Some((idx, rounded_size));
            }
        }
        None
    }

    #[cfg(not(feature = "fixed_heap"))]
    fn real_single_slot_base_size_class() -> (usize, usize) {
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            let pages = get_num_pages_by_idx(idx);
            let slot_count = match crate::sc::checked_size_class_geometry(rounded_size, pages) {
                Some((slot_count, _stride)) => slot_count,
                None => continue,
            };
            if slot_count == 1 {
                return (idx, rounded_size);
            }
        }
        panic!("test needs a base size class backed by a single-slot span");
    }

    #[cfg(feature = "fixed_heap")]
    fn move_real_non_page_aligned_objects_to_cache(
        source_cache: &mut ThreadCacheUnit,
        target_cache: &mut ThreadCacheUnit,
        idx: usize,
        rounded_size: usize,
        desired: usize,
        page_aligned: &mut alloc::vec::Vec<NonNull<u8>>,
        collected: &mut alloc::vec::Vec<NonNull<u8>>,
    ) {
        let starting_len = collected.len();
        let mut attempts = 0usize;

        while collected.len() - starting_len < desired && attempts < desired + crate::PAGE_SIZE {
            let ptr = source_cache
                .allocate(idx, align_of::<usize>())
                .expect("fixed-heap source cache should allocate real slab objects");
            attempts += 1;
            if (ptr.as_ptr() as usize) & (crate::PAGE_SIZE - 1) == 0 {
                page_aligned.push(ptr);
            } else {
                target_cache.deallocate(idx, ptr, rounded_size);
                collected.push(ptr);
            }
        }

        assert_eq!(
            collected.len() - starting_len,
            desired,
            "test needs enough real slab objects that cannot satisfy page alignment locally"
        );
    }

    #[cfg(feature = "fixed_heap")]
    fn allocate_until_bump_tail_for_test(
        cache: &mut ThreadCacheUnit,
        idx: usize,
        rounded_size: usize,
    ) -> (NonNull<u8>, alloc::vec::Vec<NonNull<u8>>) {
        let max_attempts = get_num_pages_by_idx(idx)
            .saturating_mul(crate::PAGE_SIZE)
            .checked_div(rounded_size.max(1))
            .unwrap_or(0)
            .saturating_mul(4)
            .saturating_add(crate::PAGE_SIZE / rounded_size.max(1))
            .max(1);
        let mut held = alloc::vec::Vec::new();

        for _ in 0..max_attempts {
            let ptr = cache
                .allocate(idx, align_of::<usize>())
                .expect("fixed-heap cache should allocate real slab objects");
            if cache.bump_len() != 0 {
                return (ptr, held);
            }
            held.push(ptr);
        }

        panic!("test could not reach a real slab allocation with a bump tail");
    }

    #[cfg(feature = "fixed_heap")]
    fn allocate_page_aligned_with_bump_tail_for_test(
        cache: &mut ThreadCacheUnit,
        idx: usize,
        rounded_size: usize,
    ) -> (NonNull<u8>, alloc::vec::Vec<NonNull<u8>>) {
        let max_attempts = get_num_pages_by_idx(idx)
            .saturating_mul(crate::PAGE_SIZE)
            .checked_div(rounded_size.max(1))
            .unwrap_or(0)
            .saturating_mul(4)
            .saturating_add(crate::PAGE_SIZE / rounded_size.max(1))
            .max(1);
        let mut held = alloc::vec::Vec::new();

        for _ in 0..max_attempts {
            let ptr = cache
                .allocate(idx, align_of::<usize>())
                .expect("fixed-heap cache should allocate real slab objects");
            if (ptr.as_ptr() as usize) % crate::PAGE_SIZE == 0 && cache.bump_len() != 0 {
                return (ptr, held);
            }
            held.push(ptr);
        }

        panic!("test could not reach a page-aligned real slab object with a bump tail");
    }

    #[test]
    fn thread_cache_zero_sized_allocation_uses_layout_alignment() {
        let mut cache = ThreadCache::new();
        let layout = Layout::from_size_align(0, crate::PAGE_SIZE * 2).unwrap();
        let ptr = cache
            .allocate(layout)
            .expect("zero-sized thread-cache allocation should be representable");
        assert_ne!(ptr.as_ptr() as usize, 0);
        assert_eq!((ptr.as_ptr() as usize) % layout.align(), 0);
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn thread_cache_deallocate_bypasses_local_cache_for_single_slot_base_class() {
        let (idx, rounded_size) = real_single_slot_base_size_class();
        let layout = Layout::from_size_align(rounded_size, align_of::<usize>())
            .expect("size classes are word-aligned layouts");
        let mut cache = ThreadCache::new();

        let ptr = cache
            .allocate(layout)
            .expect("thread cache should allocate a real single-slot slab object");
        assert_eq!(cache.list[idx].list.length, 0);
        assert_eq!(cache.list[idx].bump_len(), 0);

        cache.deallocate(ptr, layout);

        assert_eq!(
            cache.list[idx].list.length, 0,
            "single-slot base classes should go straight back to the slab instead of pinning one full span in a thread cache"
        );
        assert_eq!(cache.list[idx].bump_len(), 0);
        assert_eq!(cache.cached_object_bytes, 0);
        assert_eq!(cache.active_cached_class_count(), 0);
        assert!(!cache.active_cached_class_bits.is_present(idx));
    }

    #[test]
    fn thread_cache_unit_rejects_null_backend_chunk() {
        let err = ThreadCacheUnit::non_null_chunk(core::ptr::null_mut())
            .expect_err("null backend chunk must be allocation failure");
        assert_eq!(err.to_raw_errno(), AllocError::ENOMEM.to_raw_errno());
    }

    #[test]
    fn thread_cache_unit_accepts_non_null_backend_chunk() {
        let mut word = 0usize;
        let ptr = ThreadCacheUnit::non_null_chunk((&mut word as *mut usize).cast::<u8>())
            .expect("non-null backend chunk");
        assert_eq!(ptr.as_ptr(), (&mut word as *mut usize).cast::<u8>());
    }

    #[test]
    fn thread_cache_unit_rejects_invalid_backend_batch_before_deref() {
        let mut cache = ThreadCacheUnit::new();
        assert_eq!(
            cache
                .install_backend_batch((core::ptr::null_mut(), 1, None))
                .expect_err("null head must fail")
                .to_raw_errno(),
            AllocError::ENOMEM.to_raw_errno()
        );
        assert_eq!(
            cache
                .install_backend_batch((0x1000usize as *mut u8, 0, Some(8)))
                .expect_err("zero batch count must fail before bump install")
                .to_raw_errno(),
            AllocError::ESIZE.to_raw_errno()
        );
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn free_thread_cache_ignores_null_tls_pointer_without_panic() {
        unsafe {
            free_thread_cache(core::ptr::null_mut());
        }
    }

    #[test]
    fn thread_cache_unit_installs_checked_backend_batch() {
        let mut cache = ThreadCacheUnit::new();
        let mut batch = [0usize; 2];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        write_test_link_word(batch.as_mut_ptr(), second);

        let ptr = cache
            .install_backend_batch((head, 2, None))
            .expect("valid linked batch");
        assert_eq!(ptr.as_ptr(), head);
        assert_eq!(cache.list.link, second);
        assert_eq!(cache.list.length, 1);
    }

    #[test]
    fn thread_cache_unit_installs_exact_multi_node_linked_backend_batch() {
        let mut cache = ThreadCacheUnit::new();
        let mut batch = [0usize; 3];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        let third = unsafe { batch.as_mut_ptr().add(2) as usize };
        write_test_link_word(batch.as_mut_ptr(), second);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(1) }, third);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(2) }, 0);

        let ptr = cache
            .install_backend_batch((head, 3, None))
            .expect("exact null-terminated linked batch should install");

        assert_eq!(ptr.as_ptr(), head);
        assert_eq!(cache.list.link, second);
        assert_eq!(cache.list.length, 2);
    }

    #[test]
    fn thread_cache_unit_appends_multi_linked_backend_tail_to_existing_local_list() {
        let mut cache = ThreadCacheUnit::new();
        let mut cached_word = 0usize;
        let cached = (&mut cached_word as *mut usize).cast::<u8>();
        cache.free(cached);

        let mut batch = [0usize; 3];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        let third = unsafe { batch.as_mut_ptr().add(2) as usize };
        batch[0] = second;
        batch[1] = third;
        batch[2] = 0;

        let ptr = cache
            .install_backend_batch((head, 3, None))
            .expect("backend tail should be prepended to the existing local list");

        assert_eq!(ptr.as_ptr(), head);
        assert_eq!(cache.list.link, second);
        assert_eq!(cache.list.length, 3);
        assert_eq!(batch[1], third);
        assert_eq!(batch[2], cached as usize);
        assert_eq!(cached_word, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_backend_tail_append_to_corrupt_local_list() {
        let mut cache = ThreadCacheUnit::new();
        let mut local_chunks = [0usize; 2];
        let local_head = local_chunks.as_mut_ptr() as usize;
        let unexpected_extra = unsafe { local_chunks.as_mut_ptr().add(1) as usize };
        write_test_link_word(local_chunks.as_mut_ptr(), unexpected_extra);
        write_test_link_word(unsafe { local_chunks.as_mut_ptr().add(1) }, 0);
        cache.list.link = local_head;
        cache.list.length = 1;

        let mut backend = [0usize; 2];
        let head = backend.as_mut_ptr().cast::<u8>();
        let tail = unsafe { backend.as_mut_ptr().add(1) as usize };
        backend[0] = tail;
        backend[1] = 0;

        let err = cache
            .install_backend_batch((head, 2, None))
            .expect_err("corrupt existing local list must fail before appending a backend tail");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(
            backend[1], 0,
            "backend tail must not be linked to a corrupt local list"
        );
        assert_eq!(cache.list.link, local_head);
        assert_eq!(cache.list.length, 1);
    }

    #[test]
    fn thread_cache_unit_detaches_flush_suffix_after_exact_validation() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 3];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };
        let third = unsafe { chunks.as_mut_ptr().add(2).cast::<u8>() };

        cache.free(first);
        cache.free(second);
        cache.free(third);

        let batch = cache
            .detach_flush_suffix(1)
            .expect("well-formed list should detach")
            .expect("flush suffix should exist");

        assert_eq!(batch.boundary, third as usize);
        assert_eq!(batch.suffix_head, second as usize);
        assert_eq!(batch.original_length, 3);
        assert_eq!(batch.kept_length, 1);
        assert_eq!(cache.list.link, third as usize);
        assert_eq!(cache.list.length, 1);
        assert_eq!(chunks[2], 0);
        assert_eq!(chunks[1], first as usize);
        assert_eq!(chunks[0], 0);
    }

    #[test]
    fn thread_cache_unit_restore_detached_flush_suffix_rejoins_list() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 3];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };
        let third = unsafe { chunks.as_mut_ptr().add(2).cast::<u8>() };

        cache.free(first);
        cache.free(second);
        cache.free(third);

        let batch = cache
            .detach_flush_suffix(1)
            .expect("well-formed list should detach")
            .expect("flush suffix should exist");
        cache
            .restore_detached_flush_suffix(batch)
            .expect("detached suffix should restore");

        assert_eq!(cache.list.link, third as usize);
        assert_eq!(cache.list.length, 3);
        assert_eq!(chunks[2], second as usize);
        assert_eq!(cache.list.pop_unchecked_aligned(1), third);
        assert_eq!(cache.list.pop_unchecked_aligned(1), second);
        assert_eq!(cache.list.pop_unchecked_aligned(1), first);
    }

    #[test]
    fn thread_cache_unit_detach_zero_detaches_and_restores_whole_list() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 2];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };

        cache.free(first);
        cache.free(second);

        let batch = cache
            .detach_flush_suffix(0)
            .expect("well-formed whole list should detach")
            .expect("whole-list flush batch should exist");

        assert_eq!(batch.boundary, 0);
        assert_eq!(batch.suffix_head, second as usize);
        assert_eq!(batch.original_length, 2);
        assert_eq!(batch.kept_length, 0);
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
        assert_eq!(chunks[1], first as usize);
        assert_eq!(chunks[0], 0);

        cache
            .restore_detached_flush_suffix(batch)
            .expect("whole-list suffix should restore after busy slab lock");

        assert_eq!(cache.list.link, second as usize);
        assert_eq!(cache.list.length, 2);
        assert_eq!(cache.list.pop_unchecked_aligned(1), second);
        assert_eq!(cache.list.pop_unchecked_aligned(1), first);
    }

    #[test]
    fn thread_cache_unit_validates_exact_local_list_before_cleanup_handoff() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 2];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };

        cache.free(first);
        cache.free(second);

        assert!(cache.validate_local_list_exact().is_ok());
        cache.clean_up(TOTAL_SIZE_CLASS);
        assert_eq!(cache.list.link, second as usize);
        assert_eq!(cache.list.length, 2);
        assert_eq!(chunks[1], first as usize);
        assert_eq!(chunks[0], 0);
    }

    #[test]
    fn thread_cache_unit_cleanup_clears_corrupt_null_head_metadata() {
        let mut cache = ThreadCacheUnit::new();
        cache.list.link = 0;
        cache.list.length = 1;

        cache.clean_up(1);

        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_cleanup_clears_overlong_local_list_before_slab_handoff() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 2];
        let head = chunks.as_mut_ptr() as usize;
        let second = unsafe { chunks.as_mut_ptr().add(1) as usize };
        chunks[0] = second;
        chunks[1] = 0;
        cache.list.link = head;
        cache.list.length = 1;

        cache.clean_up(1);

        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
        assert_eq!(chunks[0], second);
        assert_eq!(chunks[1], 0);
    }

    #[test]
    fn thread_cache_unit_detach_rejects_misaligned_suffix_without_mutation() {
        let mut cache = ThreadCacheUnit::new();
        let mut head_word = 0x1001usize;
        let head = (&mut head_word as *mut usize).cast::<u8>();
        cache.list.link = head as usize;
        cache.list.length = 2;

        let err = cache
            .detach_flush_suffix(1)
            .expect_err("misaligned suffix must fail before deref");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, head as usize);
        assert_eq!(cache.list.length, 2);
        assert_eq!(head_word, 0x1001);
    }

    #[test]
    fn thread_cache_unit_detach_rejects_overlong_suffix_without_mutation() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 3];
        let head = chunks.as_mut_ptr() as usize;
        let second = unsafe { chunks.as_mut_ptr().add(1) as usize };
        let third = unsafe { chunks.as_mut_ptr().add(2) as usize };
        chunks[0] = second;
        chunks[1] = third;
        chunks[2] = 0;
        cache.list.link = head;
        cache.list.length = 2;

        let err = cache
            .detach_flush_suffix(1)
            .expect_err("suffix length must match local list metadata exactly");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, head);
        assert_eq!(cache.list.length, 2);
        assert_eq!(chunks[0], second);
        assert_eq!(chunks[1], third);
    }

    #[test]
    fn thread_cache_unit_rejects_tail_append_length_overflow_without_mutation() {
        let mut cache = ThreadCacheUnit::new();
        let mut cached_word = 0usize;
        let cached = (&mut cached_word as *mut usize).cast::<u8>();
        cache.list.link = cached as usize;
        cache.list.length = usize::MAX;

        let mut batch = [0usize; 2];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        write_test_link_word(batch.as_mut_ptr(), second);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(1) }, 0);

        let err = cache
            .install_backend_batch((head, 2, None))
            .expect_err("tail append must not overflow local-list length");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, cached as usize);
        assert_eq!(cache.list.length, usize::MAX);
        assert_eq!(batch[1], 0);
        assert_eq!(cached_word, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_tail_append_to_corrupt_local_list_without_mutation() {
        let mut cache = ThreadCacheUnit::new();
        cache.list.length = 1;
        cache.list.link = 0;

        let mut batch = [0usize; 2];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        write_test_link_word(batch.as_mut_ptr(), second);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(1) }, 0);

        let err = cache
            .install_backend_batch((head, 2, None))
            .expect_err("corrupt local-list metadata must fail before tail splice");

        assert_eq!(err.to_raw_errno(), AllocError::EFATAL.to_raw_errno());
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 1);
        assert_eq!(batch[1], 0);
    }

    #[test]
    fn thread_cache_unit_single_linked_backend_batch_does_not_install_phantom_tail() {
        let mut cache = ThreadCacheUnit::new();
        let mut head_word = 0usize;
        let head = (&mut head_word as *mut usize).cast::<u8>();

        let ptr = cache
            .install_backend_batch((head, 1, None))
            .expect("single linked-list batch node is valid");

        assert_eq!(ptr.as_ptr(), head);
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_single_linked_backend_batch_preserves_existing_local_list() {
        let mut cache = ThreadCacheUnit::new();
        let mut cached_word = 0usize;
        let cached = (&mut cached_word as *mut usize).cast::<u8>();
        cache.free(cached);
        assert_eq!(cache.list.link, cached as usize);
        assert_eq!(cache.list.length, 1);

        let mut head_word = 0usize;
        let head = (&mut head_word as *mut usize).cast::<u8>();
        let ptr = cache
            .install_backend_batch((head, 1, None))
            .expect("single returned linked-list slot has no tail to install");

        assert_eq!(ptr.as_ptr(), head);
        assert_eq!(cache.list.link, cached as usize);
        assert_eq!(cache.list.length, 1);
    }

    #[test]
    fn thread_cache_unit_single_bump_backend_batch_clears_stale_tail_state() {
        let mut cache = ThreadCacheUnit::new();
        cache.bump_ptr = 0x1000;
        cache.bump_unit = 8;
        cache.bump_count = 1;

        let mut head_word = 0usize;
        let head = (&mut head_word as *mut usize).cast::<u8>();
        let ptr = cache
            .install_backend_batch((head, 1, Some(8)))
            .expect("single returned bump slot has no tail to install");

        assert_eq!(ptr.as_ptr(), head);
        assert_eq!(cache.bump_ptr, 0);
        assert_eq!(cache.bump_unit, 0);
        assert_eq!(cache.bump_count, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_short_multi_node_linked_backend_batch_without_installing_tail() {
        let mut cache = ThreadCacheUnit::new();
        let mut batch = [0usize; 2];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        write_test_link_word(batch.as_mut_ptr(), second);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(1) }, 0);

        let err = cache
            .install_backend_batch((head, 3, None))
            .expect_err("returned_count must match the backend linked-list length");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_overlong_linked_backend_batch_without_phantom_tail() {
        let mut cache = ThreadCacheUnit::new();
        let mut batch = [0usize; 3];
        let head = batch.as_mut_ptr() as *mut u8;
        let second = unsafe { batch.as_mut_ptr().add(1) as usize };
        let third = unsafe { batch.as_mut_ptr().add(2) as usize };
        write_test_link_word(batch.as_mut_ptr(), second);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(1) }, third);
        write_test_link_word(unsafe { batch.as_mut_ptr().add(2) }, 0);

        let err = cache
            .install_backend_batch((head, 2, None))
            .expect_err("linked-list batch must terminate exactly at returned_count");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_truncated_linked_backend_batch_without_phantom_tail() {
        let mut cache = ThreadCacheUnit::new();
        let mut head_word = 0usize;
        let head = (&mut head_word as *mut usize).cast::<u8>();

        let err = cache
            .install_backend_batch((head, 2, None))
            .expect_err("multi-node linked-list batch cannot have a null tail");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_misaligned_linked_backend_batch_before_deref() {
        let mut cache = ThreadCacheUnit::new();
        let mut batch = [0u8; core::mem::size_of::<usize>() * 2 + 1];
        let head = unsafe { batch.as_mut_ptr().add(1) };

        let err = cache
            .install_backend_batch((head, 2, None))
            .expect_err("misaligned linked-list batch head must fail closed before deref");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.link, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_backend_batch_overflow() {
        let mut cache = ThreadCacheUnit::new();
        let mut word = 0usize;
        let head = (&mut word as *mut usize).cast::<u8>();
        let err = cache
            .install_backend_batch((head, i32::MAX as usize + 2, Some(8)))
            .expect_err("oversized bump batch must fail");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
    }

    #[test]
    fn thread_cache_unit_rejects_over_page_alignment_without_backend_touch() {
        let mut cache = ThreadCacheUnit::new();
        let err = cache
            .allocate(1, crate::PAGE_SIZE * 2)
            .expect_err("small-object cache cannot satisfy over-page alignment");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.list.length, 0);
        assert_eq!(cache.bump_count, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_zero_bump_unit() {
        let mut cache = ThreadCacheUnit::new();
        let mut word = 0usize;
        let head = (&mut word as *mut usize).cast::<u8>();
        let err = cache
            .install_backend_batch((head, 2, Some(0)))
            .expect_err("zero bump unit would repeat the same pointer");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.bump_count, 0);
        assert_eq!(cache.bump_unit, 0);
        assert_eq!(cache.bump_ptr, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_bump_span_overflow() {
        let mut cache = ThreadCacheUnit::new();
        let head = (usize::MAX - 8) as *mut u8;
        let err = cache
            .install_backend_batch((head, 3, Some(8)))
            .expect_err("entire bump span must fit in usize");
        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.bump_count, 0);
        assert_eq!(cache.bump_unit, 0);
        assert_eq!(cache.bump_ptr, 0);
    }

    #[test]
    fn thread_cache_unit_consumes_last_bump_slot_without_unneeded_overflow_advance() {
        let mut cache = ThreadCacheUnit::new();
        cache.bump_ptr = usize::MAX - 7;
        cache.bump_unit = 8;
        cache.bump_count = 1;

        let ptr = cache
            .take_bump_slot()
            .expect("last bump slot does not need a next-pointer advance");

        assert_eq!(ptr, usize::MAX - 7);
        assert_eq!(cache.bump_count, 0);
        assert_eq!(cache.bump_unit, 0);
        assert_eq!(cache.bump_ptr, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_corrupt_multi_bump_overflow_without_returning_pointer() {
        let mut cache = ThreadCacheUnit::new();
        cache.bump_ptr = usize::MAX - 7;
        cache.bump_unit = 8;
        cache.bump_count = 2;

        let err = cache
            .allocate(1, 1)
            .expect_err("corrupt bump metadata must fail closed before returning a pointer");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.bump_count, 0);
        assert_eq!(cache.bump_unit, 0);
        assert_eq!(cache.bump_ptr, 0);
        assert_eq!(cache.list.length, 0);
    }

    #[test]
    fn thread_cache_unit_rejects_zero_or_negative_bump_state() {
        let mut cache = ThreadCacheUnit::new();
        cache.bump_ptr = 0x1000;
        cache.bump_unit = -8;
        cache.bump_count = 1;

        let err = cache
            .take_bump_slot()
            .expect_err("negative bump unit is corrupt metadata");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
        assert_eq!(cache.bump_count, 0);
        assert_eq!(cache.bump_unit, 0);
        assert_eq!(cache.bump_ptr, 0);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_unit_keeps_flush_batch_when_zone_unavailable() {
        let _zone_guard = FixedHeapZoneUnavailableGuard::install();
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 2];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };

        cache.free(first);
        cache.deallocate(
            1,
            NonNull::new(second).expect("non-null chunk"),
            THREAD_CACHE_FLUSH_BYTES,
        );

        assert_eq!(cache.list.link, second as usize);
        assert_eq!(cache.list.length, 2);
        assert_eq!(chunks[1], first as usize);
        assert_eq!(chunks[0], 0);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_unit_cleanup_keeps_list_when_zone_unavailable() {
        let _zone_guard = FixedHeapZoneUnavailableGuard::install();
        let mut cache = ThreadCacheUnit::new();
        let mut chunk = 0usize;
        let ptr = (&mut chunk as *mut usize).cast::<u8>();

        cache.free(ptr);
        cache.clean_up(1);

        assert_eq!(cache.list.link, ptr as usize);
        assert_eq!(cache.list.length, 1);
        assert_eq!(chunk, 0);
    }

    #[test]
    fn thread_cache_unit_restores_flush_batch_when_slab_rejects() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 2];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };

        cache.free(first);
        cache.deallocate(
            TOTAL_SIZE_CLASS,
            NonNull::new(second).expect("non-null chunk"),
            THREAD_CACHE_FLUSH_BYTES,
        );

        assert_eq!(cache.list.link, second as usize);
        assert_eq!(cache.list.length, 2);
        assert_eq!(chunks[1], first as usize);
        assert_eq!(chunks[0], 0);
    }

    #[test]
    fn thread_cache_unit_cleanup_keeps_list_when_slab_rejects() {
        let mut cache = ThreadCacheUnit::new();
        let mut chunk = 0usize;
        let ptr = (&mut chunk as *mut usize).cast::<u8>();

        cache.free(ptr);
        cache.clean_up(TOTAL_SIZE_CLASS);

        assert_eq!(cache.list.link, ptr as usize);
        assert_eq!(cache.list.length, 1);
        assert_eq!(chunk, 0);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_soft_flush_restores_suffix_when_real_slab_lock_is_busy() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let idx = 2;
        let rounded_size = get_rounded_size_by_idx(idx);
        let soft_limit = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );
        assert!(soft_limit < THREAD_CACHE_FLUSH_OBJECTS_MAX + 1);

        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; THREAD_CACHE_FLUSH_OBJECTS_MAX + 1];
        for chunk in chunks.iter_mut().take(soft_limit) {
            cache.free((chunk as *mut usize).cast::<u8>());
        }

        let zone = crate::zone::try_global_zone().expect("fixed heap zone must be initialized");
        let _busy_slab = zone
            .test_lock_slab_for_contention(idx)
            .expect("test size class must exist");
        let new_head = (&mut chunks[soft_limit] as *mut usize).cast::<u8>();

        cache.deallocate(
            idx,
            NonNull::new(new_head).expect("test chunk is non-null"),
            rounded_size,
        );

        assert_eq!(
            cache.list.length,
            soft_limit + 1,
            "busy soft flush must restore the detached suffix instead of dropping cached objects"
        );
        assert_eq!(cache.list.link, new_head as usize);
        let previous_head = unsafe { chunks.as_ptr().add(soft_limit - 1) as usize };
        assert_eq!(chunks[soft_limit], previous_head);
        assert!(
            cache.validate_local_list_exact().is_ok(),
            "restored list must remain an exact null-terminated chain"
        );

        let mut aggregate_cache = ThreadCache::new();
        let mut aggregate_chunks = [0usize; THREAD_CACHE_FLUSH_OBJECTS_MAX + 1];
        for chunk in aggregate_chunks.iter_mut().take(soft_limit + 1) {
            aggregate_cache.list[idx].free((chunk as *mut usize).cast::<u8>());
        }
        aggregate_cache.cached_object_bytes = usize::MAX;

        assert!(
            !aggregate_cache.trim_class_to_keep_length(idx, soft_limit, false),
            "busy soft flush restores the suffix, so topology should not shrink"
        );
        assert_eq!(
            aggregate_cache.cached_object_bytes,
            usize::MAX,
            "busy restore proves ownership did not transfer and should not pay a full aggregate resync"
        );
        assert_eq!(aggregate_cache.list[idx].list.length, soft_limit + 1);
        assert!(aggregate_cache.list[idx]
            .validate_local_list_exact()
            .is_ok());

        aggregate_cache.sync_cached_object_bytes();
        assert_eq!(
            aggregate_cache.cached_object_bytes,
            aggregate_cache.recompute_cached_object_bytes(),
            "explicit repair still recomputes exact aggregate bytes when needed"
        );
        assert!(
            !aggregate_cache.trim_class_to_keep_length(idx, soft_limit, false),
            "second busy restore should still preserve the full local list"
        );
        assert_eq!(
            aggregate_cache.cached_object_bytes,
            aggregate_cache.recompute_cached_object_bytes(),
            "incremental busy-restore accounting stays exact when the cached aggregate starts exact"
        );
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_flush_error_does_not_restore_uncertain_suffix() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 3];
        let first = chunks.as_mut_ptr().cast::<u8>();
        let second = unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() };
        let third = unsafe { chunks.as_mut_ptr().add(2).cast::<u8>() };

        cache.free(first);
        cache.free(second);
        cache.deallocate(
            1,
            NonNull::new(third).expect("test chunk is non-null"),
            THREAD_CACHE_FLUSH_BYTES,
        );

        assert_eq!(
            cache.list.link, third as usize,
            "only the kept prefix should remain cacheable after a backend error"
        );
        assert_eq!(cache.list.length, 1);
        assert_eq!(
            chunks[2], 0,
            "the attempted suffix must stay detached because ownership is uncertain"
        );

        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        let mut aggregate_cache = ThreadCache::new();
        let mut aggregate_chunks = [0usize; 3];
        aggregate_cache.list[idx].free(aggregate_chunks.as_mut_ptr().cast::<u8>());
        aggregate_cache.list[idx]
            .free(unsafe { aggregate_chunks.as_mut_ptr().add(1).cast::<u8>() });
        aggregate_cache.list[idx]
            .free(unsafe { aggregate_chunks.as_mut_ptr().add(2).cast::<u8>() });
        aggregate_cache.cached_object_bytes = usize::MAX;

        assert!(
            aggregate_cache.trim_class_to_keep_length(idx, 1, true),
            "failed-after-transfer uncertainty should leave only the known kept prefix cacheable"
        );
        assert_eq!(aggregate_cache.list[idx].list.length, 1);
        assert_eq!(
            aggregate_cache.cached_object_bytes,
            aggregate_cache.recompute_cached_object_bytes(),
            "aggregate bytes must be resynced after dropping an uncertain suffix"
        );
        assert_eq!(aggregate_cache.cached_object_bytes, rounded_size);
        assert_eq!(
            aggregate_chunks[2], 0,
            "uncertain suffix must not be re-linked into the kept prefix"
        );
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_tiny_class_flushes_real_suffix_to_slab_and_keeps_hot_prefix_reusable() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        let soft_limit = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );
        let target = thread_cache_target_keep_length(soft_limit + 1, rounded_size);
        assert_eq!(rounded_size, 8);
        assert_eq!(target, THREAD_CACHE_TARGET_OBJECTS_MAX);

        let mut cache = ThreadCacheUnit::new();
        let mut chunks = alloc::vec::Vec::with_capacity(soft_limit + 1);
        for _ in 0..=soft_limit {
            chunks.push(
                cache
                    .allocate(idx, align_of::<usize>())
                    .expect("tiny fixed-heap cache should allocate from real slabs"),
            );
        }

        for ptr in chunks.iter().copied() {
            cache.deallocate(idx, ptr, rounded_size);
        }

        assert_eq!(
            cache.list.length, target,
            "real slab-backed flush should keep only the configured hot prefix"
        );
        assert!(
            cache.validate_local_list_exact().is_ok(),
            "kept hot prefix must remain a valid local free list"
        );

        let hot_head = cache.list.link;
        let local = cache
            .allocate(idx, align_of::<usize>())
            .expect("hot prefix should remain locally reusable");
        assert_eq!(
            local.as_ptr() as usize,
            hot_head,
            "local reuse should pop the retained hot-prefix head without a slab round trip"
        );
        assert_eq!(cache.list.length, target - 1);

        let suffix_cutoff = chunks.len() - target;
        let mut other_cache = ThreadCacheUnit::new();
        let remote = other_cache
            .allocate(idx, align_of::<usize>())
            .expect("another cache should allocate from the suffix returned to the slab");
        assert!(
            chunks[..suffix_cutoff]
                .iter()
                .any(|ptr| ptr.as_ptr() == remote.as_ptr()),
            "another cache should receive one of the real objects flushed back to the slab"
        );

        cache.deallocate(idx, local, rounded_size);
        cache.clean_up(idx);
        other_cache.deallocate(idx, remote, rounded_size);
        other_cache.clean_up(idx);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_alignment_miss_returns_surplus_real_slab_objects() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        let normal_target =
            thread_cache_target_keep_length(THREAD_CACHE_TARGET_OBJECTS_MAX + 1, rounded_size);
        let alignment_target =
            thread_cache_alignment_miss_keep_length(normal_target + 1, rounded_size);
        assert_eq!(rounded_size, 8);
        assert_eq!(normal_target, THREAD_CACHE_TARGET_OBJECTS_MAX);
        assert_eq!(
            alignment_target,
            THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX
        );

        let mut source_cache = ThreadCacheUnit::new();
        let mut miss_cache = ThreadCacheUnit::new();
        let mut page_aligned = alloc::vec::Vec::new();
        let mut collected = 0usize;
        let desired = normal_target + 1;
        let mut attempts = 0usize;

        while collected < desired && attempts < desired + crate::PAGE_SIZE {
            let ptr = source_cache
                .allocate(idx, align_of::<usize>())
                .expect("fixed-heap source cache should allocate real slab objects");
            attempts += 1;
            if (ptr.as_ptr() as usize) & (crate::PAGE_SIZE - 1) == 0 {
                page_aligned.push(ptr);
            } else {
                miss_cache.deallocate(idx, ptr, rounded_size);
                collected += 1;
            }
        }
        assert_eq!(
            collected, desired,
            "test needs enough real slab objects that cannot satisfy page alignment locally"
        );
        assert_eq!(miss_cache.list.length, desired);
        assert_eq!(
            miss_cache.list.pop_unchecked_aligned(crate::PAGE_SIZE),
            core::ptr::null_mut::<u8>(),
            "the prepared local list should reproduce a high-alignment cache miss"
        );

        let mut alignment_miss_streak = 0;
        let disposition =
            miss_cache.trim_after_alignment_miss(idx, rounded_size, &mut alignment_miss_streak);
        assert_eq!(disposition, FlushDisposition::ReturnedToSlab);
        assert_eq!(
            miss_cache.list.length, alignment_target,
            "alignment miss should keep only a tiny hot prefix locally"
        );
        assert!(
            miss_cache.validate_local_list_exact().is_ok(),
            "kept prefix must remain a valid local free list after real slab handoff"
        );

        let mut other_cache = ThreadCacheUnit::new();
        let remote = other_cache
            .allocate(idx, align_of::<usize>())
            .expect("slab should remain usable after receiving the surplus suffix");

        other_cache.deallocate(idx, remote, rounded_size);
        other_cache.clean_up(idx);
        for ptr in page_aligned {
            source_cache.deallocate(idx, ptr, rounded_size);
        }
        source_cache.clean_up(idx);
        miss_cache.clean_up(idx);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_alignment_miss_large_class_uses_byte_target_for_real_slab_objects() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let (idx, rounded_size) = real_subpage_large_size_class();
        let alignment_target = thread_cache_alignment_miss_keep_length(usize::MAX, rounded_size);
        assert!(
            alignment_target < THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
            "large size class should be byte-capped below the object compatibility target"
        );
        assert_eq!(
            alignment_target,
            thread_cache_alignment_miss_object_limit(
                rounded_size,
                THREAD_CACHE_ALIGNMENT_MISS_TARGET_BYTES,
                THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
            )
        );

        let mut source_cache = ThreadCacheUnit::new();
        let mut miss_cache = ThreadCacheUnit::new();
        let mut page_aligned = alloc::vec::Vec::new();
        let mut collected = alloc::vec::Vec::new();
        let desired = alignment_target + 1;

        move_real_non_page_aligned_objects_to_cache(
            &mut source_cache,
            &mut miss_cache,
            idx,
            rounded_size,
            desired,
            &mut page_aligned,
            &mut collected,
        );

        assert_eq!(miss_cache.list.length, desired);
        assert_eq!(
            miss_cache.list.pop_unchecked_aligned(crate::PAGE_SIZE),
            core::ptr::null_mut::<u8>(),
            "the prepared large-class list should reproduce a page-alignment cache miss"
        );

        let mut alignment_miss_streak = 0;
        let disposition =
            miss_cache.trim_after_alignment_miss(idx, rounded_size, &mut alignment_miss_streak);
        assert_eq!(disposition, FlushDisposition::ReturnedToSlab);
        assert_eq!(
            miss_cache.list.length, alignment_target,
            "large strict-alignment miss should retain only the byte-aware hot prefix"
        );
        assert_eq!(alignment_miss_streak, 1);
        assert!(
            miss_cache.validate_local_list_exact().is_ok(),
            "kept large-class prefix must remain a valid local free list"
        );

        let returned_candidate_limit = collected.len() - alignment_target;
        let mut other_cache = ThreadCacheUnit::new();
        let remote = other_cache
            .allocate(idx, align_of::<usize>())
            .expect("another cache should allocate from large-class surplus returned to the slab");
        assert!(
            collected[..returned_candidate_limit]
                .iter()
                .any(|ptr| ptr.as_ptr() == remote.as_ptr()),
            "another cache should receive the large-class suffix returned after the alignment miss"
        );

        other_cache.deallocate(idx, remote, rounded_size);
        other_cache.clean_up(idx);
        for ptr in page_aligned {
            source_cache.deallocate(idx, ptr, rounded_size);
        }
        source_cache.clean_up(idx);
        miss_cache.clean_up(idx);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_alignment_miss_repeated_large_subpage_class_can_retain_zero() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let (idx, rounded_size) = match real_subpage_size_class_larger_than(
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_BYTES,
        ) {
            Some(size_class) => size_class,
            None => {
                println!(
                    "skipping repeated large-subpage alignment-miss zero-retention regression: \
                     no size class is > repeated byte budget ({}) and < PAGE_SIZE ({}) on this target",
                    THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_BYTES,
                    crate::PAGE_SIZE
                );
                return;
            }
        };
        let repeated_target = thread_cache_repeated_alignment_miss_keep_length(1, rounded_size, 2);
        assert_eq!(
            repeated_target, 0,
            "repeated strict-alignment miss should be a true byte cap for oversized classes"
        );

        let mut source_cache = ThreadCacheUnit::new();
        let mut miss_cache = ThreadCacheUnit::new();
        let mut page_aligned = alloc::vec::Vec::new();
        let mut collected = alloc::vec::Vec::new();

        move_real_non_page_aligned_objects_to_cache(
            &mut source_cache,
            &mut miss_cache,
            idx,
            rounded_size,
            1,
            &mut page_aligned,
            &mut collected,
        );

        assert_eq!(miss_cache.list.length, 1);
        assert_eq!(
            miss_cache.list.pop_unchecked_aligned(crate::PAGE_SIZE),
            core::ptr::null_mut::<u8>(),
            "prepared oversized-class object should reproduce a page-alignment cache miss"
        );

        let mut alignment_miss_streak = 1;
        let disposition =
            miss_cache.trim_after_alignment_miss(idx, rounded_size, &mut alignment_miss_streak);
        assert_eq!(disposition, FlushDisposition::ReturnedToSlab);
        assert_eq!(alignment_miss_streak, 2);
        assert_eq!(
            miss_cache.list.length, 0,
            "zero repeated-miss target should return the whole oversized local list"
        );

        let mut other_cache = ThreadCacheUnit::new();
        let remote = other_cache
            .allocate(idx, align_of::<usize>())
            .expect("another cache should reuse the whole-list object returned to the slab");
        assert_eq!(
            remote.as_ptr(),
            collected[0].as_ptr(),
            "the single returned oversized object should be available to another cache"
        );

        other_cache.deallocate(idx, remote, rounded_size);
        other_cache.clean_up(idx);
        for ptr in page_aligned {
            source_cache.deallocate(idx, ptr, rounded_size);
        }
        source_cache.clean_up(idx);
        miss_cache.clean_up(idx);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_repeated_alignment_miss_strongly_trims_real_slab_objects() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        let first_fill = THREAD_CACHE_TARGET_OBJECTS_MAX + 1;
        let first_target = thread_cache_alignment_miss_keep_length(first_fill, rounded_size);
        let second_fill = THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_OBJECTS_MAX + 1;
        let repeated_target = thread_cache_repeated_alignment_miss_keep_length(
            first_target + second_fill,
            rounded_size,
            2,
        );

        assert_eq!(rounded_size, 8);
        assert_eq!(first_target, THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX);
        assert_eq!(
            repeated_target,
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_OBJECTS_MAX
        );

        let mut source_cache = ThreadCacheUnit::new();
        let mut miss_cache = ThreadCacheUnit::new();
        let mut page_aligned = alloc::vec::Vec::new();
        let mut collected = alloc::vec::Vec::new();

        move_real_non_page_aligned_objects_to_cache(
            &mut source_cache,
            &mut miss_cache,
            idx,
            rounded_size,
            first_fill,
            &mut page_aligned,
            &mut collected,
        );

        assert_eq!(miss_cache.list.length, first_fill);
        assert_eq!(
            miss_cache.list.pop_unchecked_aligned(crate::PAGE_SIZE),
            core::ptr::null_mut::<u8>(),
            "the prepared local list should reproduce a first high-alignment cache miss"
        );

        let mut alignment_miss_streak = 0;
        let first_disposition =
            miss_cache.trim_after_alignment_miss(idx, rounded_size, &mut alignment_miss_streak);
        assert_eq!(first_disposition, FlushDisposition::ReturnedToSlab);
        assert_eq!(
            miss_cache.list.length, first_target,
            "the first strict-alignment miss must preserve the historical 32-object target"
        );
        assert_eq!(alignment_miss_streak, 1);
        assert!(
            miss_cache.validate_local_list_exact().is_ok(),
            "first kept prefix must remain a valid local free list"
        );

        let mut first_surplus_holder_cache = ThreadCacheUnit::new();
        let first_surplus_count = first_fill - first_target;
        let mut first_surplus_holders = alloc::vec::Vec::with_capacity(first_surplus_count);
        for _ in 0..first_surplus_count {
            first_surplus_holders.push(
                first_surplus_holder_cache
                    .allocate(idx, align_of::<usize>())
                    .expect("first-trim surplus should be reusable by another real cache"),
            );
        }

        let second_collected_start = collected.len();
        move_real_non_page_aligned_objects_to_cache(
            &mut source_cache,
            &mut miss_cache,
            idx,
            rounded_size,
            second_fill,
            &mut page_aligned,
            &mut collected,
        );

        assert_eq!(miss_cache.list.length, first_target + second_fill);
        assert_eq!(
            miss_cache.list.pop_unchecked_aligned(crate::PAGE_SIZE),
            core::ptr::null_mut::<u8>(),
            "refilled local list should reproduce a second consecutive high-alignment miss"
        );

        let second_disposition =
            miss_cache.trim_after_alignment_miss(idx, rounded_size, &mut alignment_miss_streak);
        assert_eq!(second_disposition, FlushDisposition::ReturnedToSlab);
        assert_eq!(
            miss_cache.list.length, repeated_target,
            "the second consecutive strict-alignment miss must trim down to four hot objects"
        );
        assert_eq!(alignment_miss_streak, 2);
        assert!(
            miss_cache.validate_local_list_exact().is_ok(),
            "second kept prefix must remain a valid local free list"
        );

        let second_collected = &collected[second_collected_start..];
        let second_returned_candidate_limit = second_collected
            .len()
            .checked_sub(repeated_target)
            .expect("second refill must exceed the repeated-miss retained prefix");
        assert!(
            second_returned_candidate_limit > 0,
            "test must create second-trim surplus independent of the first trim"
        );
        let mut other_cache = ThreadCacheUnit::new();
        let mut remote_holders = alloc::vec::Vec::new();
        let probe_limit = (crate::PAGE_SIZE / rounded_size).max(1);
        let mut found_second_surplus = false;
        for _ in 0..probe_limit {
            let remote = other_cache
                .allocate(idx, align_of::<usize>())
                .expect("another cache should allocate from surplus returned to the slab");
            found_second_surplus = second_collected[..second_returned_candidate_limit]
                .iter()
                .any(|ptr| ptr.as_ptr() == remote.as_ptr());
            remote_holders.push(remote);
            if found_second_surplus {
                break;
            }
        }
        assert!(
            found_second_surplus,
            "another cache should reach an object from the second refill's surplus within one page-local bitmap probe"
        );

        for ptr in remote_holders {
            other_cache.deallocate(idx, ptr, rounded_size);
        }
        other_cache.clean_up(idx);
        for ptr in first_surplus_holders {
            first_surplus_holder_cache.deallocate(idx, ptr, rounded_size);
        }
        first_surplus_holder_cache.clean_up(idx);
        for ptr in page_aligned {
            source_cache.deallocate(idx, ptr, rounded_size);
        }
        source_cache.clean_up(idx);
        miss_cache.clean_up(idx);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_bump_alignment_miss_returns_surplus_real_slab_objects() {
        let _guard = fixed_heap_thread_cache_test_guard();
        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        assert_eq!(rounded_size, 8);

        let mut cache = ThreadCacheUnit::new();
        let (first, held_before_first) =
            allocate_page_aligned_with_bump_tail_for_test(&mut cache, idx, rounded_size);
        assert_eq!(first.as_ptr() as usize % crate::PAGE_SIZE, 0);

        let remaining_bump = cache.bump_count as usize;
        let normal_target = thread_cache_target_keep_length(remaining_bump, rounded_size);
        let alignment_target =
            thread_cache_alignment_miss_keep_length(remaining_bump, rounded_size);
        assert!(alignment_target <= normal_target);
        if remaining_bump <= alignment_target {
            cache.deallocate(idx, first, rounded_size);
            for ptr in held_before_first {
                cache.deallocate(idx, ptr, rounded_size);
            }
            cache.clean_up(idx);
            return;
        }

        let high_aligned = cache
            .allocate(idx, crate::PAGE_SIZE)
            .expect("page-aligned allocation should fall back to a real slab after trimming");

        assert_eq!(high_aligned.as_ptr() as usize % crate::PAGE_SIZE, 0);
        assert_eq!(
            cache.list.length, alignment_target,
            "bump alignment miss should keep only a tiny hot prefix before requesting another slab"
        );
        assert!(
            cache.validate_local_list_exact().is_ok(),
            "kept bump-prefix list must remain valid after trimming the surplus suffix"
        );

        let mut other_cache = ThreadCacheUnit::new();
        let remote = other_cache
            .allocate(idx, align_of::<usize>())
            .expect("slab should accept the surplus bump suffix for reuse by other caches");

        cache.deallocate(idx, first, rounded_size);
        cache.deallocate(idx, high_aligned, rounded_size);
        for ptr in held_before_first {
            cache.deallocate(idx, ptr, rounded_size);
        }
        cache.clean_up(idx);
        other_cache.deallocate(idx, remote, rounded_size);
        other_cache.clean_up(idx);
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_strict_alignment_backend_batch_is_capped_on_real_slab() {
        let _guard = fixed_heap_thread_cache_test_guard();
        // Use a size class that the other fixed-heap thread-cache regressions
        // do not mutate.  The fixed-heap zone is process-global and Rust tests
        // run concurrently by default, so sharing idx=1 here would make this
        // isolation test perturb unrelated suffix-flush assertions.
        let idx = 2;
        let rounded_size = get_rounded_size_by_idx(idx);
        let strict_align = rounded_size * 2;
        let expected_cached = crate::page::STRICT_ALIGNMENT_BATCH_RETURN_LIMIT - 1;
        assert!(strict_align <= crate::PAGE_SIZE);

        let mut first_cache = ThreadCacheUnit::new();
        let first = first_cache
            .allocate(idx, strict_align)
            .expect("strict-alignment request should allocate from a real slab");

        assert_eq!(first.as_ptr() as usize & (strict_align - 1), 0);
        assert_eq!(
            first_cache.list.length, expected_cached,
            "a strict-alignment backend batch should not hoard every aligned slot in one cache"
        );
        assert!(
            first_cache.validate_local_list_exact().is_ok(),
            "capped strict-alignment tail must remain a valid linked list"
        );

        let mut second_cache = ThreadCacheUnit::new();
        let second = second_cache
            .allocate(idx, strict_align)
            .expect("remaining aligned slots should stay available in the real slab");

        assert_eq!(second.as_ptr() as usize & (strict_align - 1), 0);
        assert_eq!(
            (first.as_ptr() as usize) & !(crate::PAGE_SIZE - 1),
            (second.as_ptr() as usize) & !(crate::PAGE_SIZE - 1),
            "a second cache should reuse the same partially free object page instead of forcing a new page"
        );
        assert_eq!(second_cache.list.length, expected_cached);

        first_cache.deallocate(idx, first, rounded_size);
        first_cache.clean_up(idx);
        second_cache.deallocate(idx, second, rounded_size);
        second_cache.clean_up(idx);
    }

    #[test]
    fn thread_cache_unit_layout_keeps_alignment_miss_state_out_of_unit() {
        assert_eq!(
            core::mem::size_of::<ThreadCacheUnit>(),
            core::mem::size_of::<Linklist>()
                + core::mem::size_of::<usize>()
                + 2 * core::mem::size_of::<i32>(),
            "per-class units should stay at the baseline list + bump metadata footprint"
        );
        assert_eq!(
            core::mem::size_of::<AlignmentMissStreaks>(),
            ALIGNMENT_MISS_STREAK_WORDS * core::mem::size_of::<usize>()
        );
        assert!(
            core::mem::size_of::<AlignmentMissStreaks>()
                < core::mem::size_of::<[u8; TOTAL_SIZE_CLASS]>(),
            "2-bit packed state should stay smaller than one byte per class"
        );
    }

    #[test]
    fn alignment_miss_streaks_saturate_and_reset_per_class() {
        let mut streaks = AlignmentMissStreaks::new();
        assert_eq!(streaks.get(1), 0);
        assert_eq!(streaks.record_miss(1), 1);
        assert_eq!(streaks.record_miss(1), 2);
        assert_eq!(streaks.record_miss(1), 2);
        assert_eq!(streaks.get(2), 0);
        streaks.reset(1);
        assert_eq!(streaks.get(1), 0);
    }

    #[test]
    fn alignment_miss_streaks_boundaries_do_not_cross_slots() {
        let indices = [
            ALIGNMENT_MISS_STREAKS_PER_WORD - 1,
            ALIGNMENT_MISS_STREAKS_PER_WORD,
            TOTAL_SIZE_CLASS - 1,
        ];

        for &idx in indices.iter().filter(|&&idx| idx < TOTAL_SIZE_CLASS) {
            let mut streaks = AlignmentMissStreaks::new();
            assert_eq!(streaks.get(idx), 0);
            assert_eq!(streaks.record_miss(idx), 1);
            assert_eq!(streaks.get(idx), 1);

            for &other in indices
                .iter()
                .filter(|&&other| other < TOTAL_SIZE_CLASS && other != idx)
            {
                assert_eq!(
                    streaks.get(other),
                    0,
                    "record_miss({idx}) must not affect slot {other}"
                );
            }

            streaks.set(idx, 2);
            assert_eq!(streaks.get(idx), 2);
            assert_eq!(streaks.record_miss(idx), 2);

            for &other in indices
                .iter()
                .filter(|&&other| other < TOTAL_SIZE_CLASS && other != idx)
            {
                assert_eq!(
                    streaks.get(other),
                    0,
                    "set({idx}) must not affect slot {other}"
                );
            }

            streaks.reset(idx);
            assert_eq!(streaks.get(idx), 0);

            for &other in indices
                .iter()
                .filter(|&&other| other < TOTAL_SIZE_CLASS && other != idx)
            {
                assert_eq!(
                    streaks.get(other),
                    0,
                    "reset({idx}) must not affect slot {other}"
                );
            }
        }

        let mut streaks = AlignmentMissStreaks::new();
        streaks.set(TOTAL_SIZE_CLASS - 1, 2);
        let words = streaks.words;
        assert_eq!(streaks.record_miss(TOTAL_SIZE_CLASS), 0);
        assert_eq!(streaks.get(TOTAL_SIZE_CLASS), 0);
        assert_eq!(streaks.words, words);
        assert_eq!(streaks.get(TOTAL_SIZE_CLASS - 1), 2);
    }

    #[test]
    fn thread_cache_unit_alignment_miss_with_invalid_index_fails_closed() {
        #[cfg(feature = "fixed_heap")]
        let _guard = fixed_heap_thread_cache_test_guard();
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; 2];
        let mut ptr = chunks.as_mut_ptr().cast::<u8>();
        if (ptr as usize) & (crate::PAGE_SIZE - 1) == 0 {
            ptr = unsafe { ptr.add(core::mem::size_of::<usize>()) };
        }
        cache.free(ptr);

        let err = cache
            .allocate(TOTAL_SIZE_CLASS, crate::PAGE_SIZE)
            .expect_err("invalid size-class index must not index the size-class table");

        assert_eq!(err.to_raw_errno(), AllocError::ESIZE.to_raw_errno());
    }

    #[test]
    fn thread_cache_should_flush_ignores_zero_sized_classes() {
        assert!(!thread_cache_should_flush(usize::MAX, 0));
    }

    #[test]
    fn thread_cache_should_flush_keeps_single_entry_lists_local() {
        assert!(!thread_cache_should_flush(1, usize::MAX));
    }

    #[test]
    fn thread_cache_should_flush_preserves_threshold_boundary() {
        let rounded_size = 64;
        let boundary = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );

        assert!(!thread_cache_should_flush(boundary, rounded_size));
        assert!(thread_cache_should_flush(boundary + 1, rounded_size));
    }

    #[test]
    fn thread_cache_should_flush_caps_tiny_size_classes_by_object_count() {
        let rounded_size = 8;

        assert_eq!(
            thread_cache_object_limit(
                rounded_size,
                THREAD_CACHE_FLUSH_BYTES,
                THREAD_CACHE_FLUSH_OBJECTS_MAX,
            ),
            THREAD_CACHE_FLUSH_OBJECTS_MAX
        );
        assert!(!thread_cache_should_flush(
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
            rounded_size
        ));
        assert!(thread_cache_should_flush(
            THREAD_CACHE_FLUSH_OBJECTS_MAX + 1,
            rounded_size
        ));
    }

    #[test]
    fn thread_cache_target_keep_length_keeps_small_hot_set_after_flush() {
        let rounded_size = 8;
        let overflowing_length = THREAD_CACHE_FLUSH_OBJECTS_MAX + 1;

        assert_eq!(
            thread_cache_target_keep_length(overflowing_length, rounded_size),
            THREAD_CACHE_TARGET_OBJECTS_MAX
        );
    }

    #[test]
    fn thread_cache_target_keep_length_keeps_one_large_object() {
        let rounded_size = THREAD_CACHE_FLUSH_BYTES * 2;

        assert_eq!(thread_cache_target_keep_length(2, rounded_size), 1);
    }

    #[test]
    fn thread_cache_aggregate_pressure_keep_length_returns_only_excess_objects() {
        let current_total = THREAD_CACHE_TOTAL_TARGET_BYTES + 513;
        assert_eq!(
            thread_cache_aggregate_pressure_keep_length(
                current_total,
                THREAD_CACHE_TOTAL_TARGET_BYTES,
                10,
                256
            ),
            7,
            "513 excess bytes at 256-byte objects should return ceil(513/256)=3 objects"
        );
        assert_eq!(
            thread_cache_aggregate_pressure_keep_length(
                THREAD_CACHE_TOTAL_TARGET_BYTES + 4096,
                THREAD_CACHE_TOTAL_TARGET_BYTES,
                3,
                1024,
            ),
            0,
            "aggregate pressure may return a whole class when excess exceeds its length"
        );
        assert_eq!(
            thread_cache_aggregate_pressure_keep_length(
                THREAD_CACHE_TOTAL_TARGET_BYTES,
                THREAD_CACHE_TOTAL_TARGET_BYTES,
                7,
                256
            ),
            7,
            "no aggregate pressure should keep the class unchanged"
        );
        assert_eq!(
            thread_cache_aggregate_pressure_keep_length(
                THREAD_CACHE_TOTAL_TARGET_BYTES + 1,
                THREAD_CACHE_TOTAL_TARGET_BYTES,
                0,
                256,
            ),
            0
        );
        assert_eq!(
            thread_cache_aggregate_pressure_keep_length(
                THREAD_CACHE_TOTAL_TARGET_BYTES + 1,
                THREAD_CACHE_TOTAL_TARGET_BYTES,
                7,
                0
            ),
            7
        );
    }

    #[test]
    fn thread_cache_total_pressure_budget_selects_dense_only_for_many_active_classes() {
        assert_eq!(
            thread_cache_total_pressure_budget(THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD - 1),
            (
                THREAD_CACHE_TOTAL_FLUSH_BYTES,
                THREAD_CACHE_TOTAL_TARGET_BYTES
            ),
            "sparse/single-hot aggregate pressure should retain the normal locality budget"
        );
        assert_eq!(
            thread_cache_total_pressure_budget(THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD),
            (
                THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES,
                THREAD_CACHE_TOTAL_DENSE_TARGET_BYTES,
            ),
            "dense cross-class pressure should use the tighter retained-byte budget"
        );
    }

    #[test]
    fn thread_cache_total_blocking_limit_tracks_dense_budget() {
        assert_eq!(
            thread_cache_total_blocking_flush_limit(THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD - 1),
            THREAD_CACHE_TOTAL_FLUSH_BYTES * THREAD_CACHE_TOTAL_BLOCKING_FLUSH_MULTIPLIER,
            "sparse workloads keep the normal aggregate hard cap"
        );
        assert_eq!(
            thread_cache_total_blocking_flush_limit(THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD),
            THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES * THREAD_CACHE_TOTAL_BLOCKING_FLUSH_MULTIPLIER,
            "dense cross-class pressure should block at the dense cap instead of the old sparse cap"
        );
        assert!(
            thread_cache_total_blocking_flush_limit(THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD)
                < thread_cache_total_blocking_flush_limit(
                    THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD - 1
                ),
            "dense hard cap must reduce the lock-busy retained-memory ceiling"
        );
    }

    #[test]
    fn thread_cache_alignment_miss_keep_length_is_tighter_than_normal_hot_target() {
        let rounded_size = 8;
        let overflowing_length = THREAD_CACHE_FLUSH_OBJECTS_MAX + 1;

        assert_eq!(
            thread_cache_target_keep_length(overflowing_length, rounded_size),
            THREAD_CACHE_TARGET_OBJECTS_MAX
        );
        assert_eq!(
            thread_cache_alignment_miss_keep_length(overflowing_length, rounded_size),
            THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX
        );
        assert_eq!(
            thread_cache_repeated_alignment_miss_keep_length(overflowing_length, rounded_size, 1),
            THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
            "first strict-alignment miss should keep the compatibility target"
        );
        assert_eq!(
            thread_cache_repeated_alignment_miss_keep_length(overflowing_length, rounded_size, 2),
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
            "repeated strict-alignment misses should keep only a minimal hot prefix"
        );
        assert_eq!(
            thread_cache_alignment_miss_keep_length(8, rounded_size),
            thread_cache_target_keep_length(8, rounded_size),
            "small lists should keep the same local hot prefix as the normal trim path"
        );

        let large_rounded_size = 1024;
        let large_length = 100;
        let large_first_target = thread_cache_alignment_miss_object_limit(
            large_rounded_size,
            THREAD_CACHE_ALIGNMENT_MISS_TARGET_BYTES,
            THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
        );
        let large_repeated_target = thread_cache_alignment_miss_object_limit(
            large_rounded_size,
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_BYTES,
            THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
        );
        assert_eq!(large_first_target, 16);
        assert_eq!(large_repeated_target, 4);
        assert_eq!(
            thread_cache_alignment_miss_keep_length(large_length, large_rounded_size),
            large_first_target,
            "large size classes should be capped by the first-miss byte budget"
        );
        assert_eq!(
            thread_cache_repeated_alignment_miss_keep_length(large_length, large_rounded_size, 2),
            large_repeated_target,
            "repeated large-size misses should be capped by the tighter byte budget"
        );

        let repeated_oversized = THREAD_CACHE_REPEATED_ALIGNMENT_MISS_TARGET_BYTES + 1;
        assert_eq!(
            thread_cache_repeated_alignment_miss_keep_length(1, repeated_oversized, 2),
            0,
            "repeated strict-alignment byte cap should retain zero oversized objects"
        );

        let first_oversized = THREAD_CACHE_ALIGNMENT_MISS_TARGET_BYTES + 1;
        assert_eq!(
            thread_cache_alignment_miss_keep_length(1, first_oversized),
            0,
            "first strict-alignment byte cap should retain zero oversized objects"
        );

        assert_eq!(
            thread_cache_object_limit(
                first_oversized,
                THREAD_CACHE_ALIGNMENT_MISS_TARGET_BYTES,
                THREAD_CACHE_ALIGNMENT_MISS_TARGET_OBJECTS_MAX,
            ),
            1,
            "normal cache object-limit helper should keep its at-least-one semantics"
        );
    }

    #[test]
    fn thread_cache_total_budget_is_tighter_than_per_class_soft_sum() {
        let mut per_class_soft_sum = 0usize;
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            per_class_soft_sum = per_class_soft_sum.saturating_add(
                thread_cache_object_limit(
                    rounded_size,
                    THREAD_CACHE_FLUSH_BYTES,
                    THREAD_CACHE_FLUSH_OBJECTS_MAX,
                )
                .saturating_mul(rounded_size),
            );
        }

        assert!(THREAD_CACHE_TOTAL_TARGET_BYTES < THREAD_CACHE_TOTAL_FLUSH_BYTES);
        assert!(
            THREAD_CACHE_TOTAL_FLUSH_BYTES < per_class_soft_sum,
            "aggregate budget should cap the cross-class idle-thread footprint"
        );
    }

    #[test]
    fn thread_cache_recomputes_cached_object_bytes_from_list_and_bump_lengths() {
        let mut cache = ThreadCache::new();
        let mut chunks = [0usize; 3];
        cache.list[1].free(chunks.as_mut_ptr().cast::<u8>());
        cache.list[2].free(unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() });
        cache.list[2].free(unsafe { chunks.as_mut_ptr().add(2).cast::<u8>() });
        cache.list[3].bump_count = 2;
        cache.list[3].bump_unit = get_rounded_size_by_idx(3) as i32;
        cache.list[3].bump_ptr = chunks.as_mut_ptr() as usize;

        let expected = get_rounded_size_by_idx(1)
            + 2 * get_rounded_size_by_idx(2)
            + 2 * get_rounded_size_by_idx(3);
        assert_eq!(cache.recompute_cached_object_bytes(), expected);
        assert_eq!(cache.recompute_cached_object_state(), (expected, 3));

        cache.cached_object_bytes = usize::MAX;
        cache.active_cached_classes = usize::MAX;
        cache.sync_cached_object_bytes();
        assert_eq!(cache.cached_object_bytes, expected);
        assert_eq!(cache.active_cached_class_count(), 3);
        assert!(cache.active_cached_class_bits.is_present(1));
        assert!(cache.active_cached_class_bits.is_present(2));
        assert!(cache.active_cached_class_bits.is_present(3));
        assert_eq!(
            cache.previous_active_cached_class_idx(TOTAL_SIZE_CLASS - 1),
            Some(3)
        );
    }

    #[test]
    fn thread_cache_footprint_snapshot_reports_exact_budget_state() {
        let mut cache = ThreadCache::new();
        let small_idx = 1;
        let large_idx = 2;
        let small_size = get_rounded_size_by_idx(small_idx);
        let large_size = get_rounded_size_by_idx(large_idx);
        let mut small_chunks = [0usize; 3];
        let mut large_chunks = [0usize; 2];

        for chunk in small_chunks.iter_mut() {
            cache.list[small_idx].free((chunk as *mut usize).cast::<u8>());
        }
        for chunk in large_chunks.iter_mut() {
            cache.list[large_idx].free((chunk as *mut usize).cast::<u8>());
        }
        cache.sync_cached_object_bytes();

        let small_bytes = small_chunks.len().saturating_mul(small_size);
        let large_bytes = large_chunks.len().saturating_mul(large_size);
        let exact = small_bytes.saturating_add(large_bytes);
        let snapshot = cache.footprint_snapshot();

        assert!(snapshot.cache_initialized);
        assert_eq!(snapshot.cached_object_bytes, exact);
        assert_eq!(snapshot.exact_cached_object_bytes, exact);
        assert_eq!(snapshot.active_cached_classes, 2);
        assert_eq!(snapshot.exact_active_cached_classes, 2);
        assert_eq!(
            snapshot.largest_retained_class_bytes,
            core::cmp::max(small_bytes, large_bytes)
        );
        assert_eq!(
            snapshot.largest_retained_class_idx,
            if small_bytes >= large_bytes {
                small_idx
            } else {
                large_idx
            }
        );
        assert_eq!(
            (snapshot.flush_bytes, snapshot.target_bytes),
            thread_cache_total_pressure_budget(2)
        );
        assert_eq!(
            snapshot.hard_flush_bytes,
            thread_cache_total_blocking_flush_limit(2)
        );
        assert!(snapshot.accounting_matches_exact);
        assert_eq!(
            snapshot.over_soft_budget,
            snapshot.exact_cached_object_bytes > snapshot.flush_bytes
        );
        assert_eq!(
            snapshot.over_target_budget,
            snapshot.exact_cached_object_bytes > snapshot.target_bytes
        );

        cache.cached_object_bytes = 0;
        cache.active_cached_classes = 0;
        let stale = cache.footprint_snapshot();
        assert_eq!(stale.exact_cached_object_bytes, exact);
        assert_eq!(stale.exact_active_cached_classes, 2);
        assert!(!stale.accounting_matches_exact);
    }

    #[test]
    fn thread_cache_public_footprint_snapshot_reports_initialization_state() {
        let snapshot = crate::thread_cache_footprint_snapshot();

        if snapshot.cache_initialized {
            assert_eq!(
                snapshot.over_soft_budget,
                snapshot.exact_cached_object_bytes > snapshot.flush_bytes
            );
            assert_eq!(
                snapshot.over_target_budget,
                snapshot.exact_cached_object_bytes > snapshot.target_bytes
            );
        } else {
            assert_eq!(snapshot, crate::ThreadCacheFootprintSnapshot::default());
        }
    }

    #[test]
    fn thread_cache_active_bitmap_tracks_sparse_classes_descending() {
        let mut cache = ThreadCache::new();
        let mut chunks = [0usize; 2];
        let low_idx = 1;
        let high_idx = TOTAL_SIZE_CLASS - 1;

        cache.list[low_idx].free(chunks.as_mut_ptr().cast::<u8>());
        cache.list[high_idx].free(unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() });
        cache.sync_cached_object_bytes();

        assert_eq!(cache.active_cached_class_count(), 2);
        assert!(cache.active_cached_class_bits.is_present(low_idx));
        assert!(cache.active_cached_class_bits.is_present(high_idx));
        assert_eq!(
            cache.previous_active_cached_class_idx(TOTAL_SIZE_CLASS - 1),
            Some(high_idx),
            "descending aggregate trim should jump to the highest active class"
        );
        assert_eq!(
            cache.previous_active_cached_class_idx(high_idx - 1),
            Some(low_idx),
            "empty class gaps should be skipped by bitmap lookup"
        );

        let before = ThreadCache::class_cached_object_bytes(high_idx, &cache.list[high_idx]);
        cache.list[high_idx].clear_local_list();
        let after = ThreadCache::class_cached_object_bytes(high_idx, &cache.list[high_idx]);
        cache.account_class_cached_object_change(high_idx, before, after);

        assert_eq!(cache.active_cached_class_count(), 1);
        assert!(!cache.active_cached_class_bits.is_present(high_idx));
        assert_eq!(
            cache.previous_active_cached_class_idx(TOTAL_SIZE_CLASS - 1),
            Some(low_idx),
            "clearing the high class must immediately remove it from aggregate-trim scans"
        );
    }

    #[test]
    fn thread_cache_cleanup_repairs_stale_active_bitmap_and_clears_streaks() {
        let mut cache = ThreadCache::new();
        let idx = 1;

        // Simulate a defensive/test path that mutated the unit directly and did
        // not update the aggregate bitmap.  The list metadata is intentionally
        // invalid, so cleanup must clear it locally before any slab handoff.
        cache.list[idx].list.length = 1;
        cache.alignment_miss_streaks.set(idx, 2);
        assert_eq!(cache.cached_object_bytes, 0);
        assert_eq!(cache.active_cached_class_count(), 0);
        assert!(!cache.active_cached_class_bits.is_present(idx));

        cache.cleanup_cache_unchecked();

        assert_eq!(cache.list[idx].list.length, 0);
        assert_eq!(cache.list[idx].list.link, 0);
        assert_eq!(cache.cached_object_bytes, 0);
        assert_eq!(cache.active_cached_class_count(), 0);
        assert!(!cache.active_cached_class_bits.is_present(idx));
        assert_eq!(cache.alignment_miss_streaks.get(idx), 0);
    }

    #[test]
    fn thread_cache_largest_retained_class_prefers_bytes_over_index() {
        let mut cache = ThreadCache::new();
        let low_idx = 1;
        let high_idx = TOTAL_SIZE_CLASS - 1;
        let low_size = get_rounded_size_by_idx(low_idx);
        let high_size = get_rounded_size_by_idx(high_idx);
        assert!(low_size != 0);
        assert!(
            high_size > low_size,
            "test expects the last class to be larger than the first"
        );

        let low_count = high_size / low_size + 2;
        let mut low_chunks = alloc::vec::Vec::new();
        for _ in 0..low_count {
            low_chunks.push(0usize);
        }
        for chunk in &mut low_chunks {
            cache.list[low_idx].free((chunk as *mut usize).cast::<u8>());
        }
        let mut high_chunk = 0usize;
        cache.list[high_idx].free((&mut high_chunk as *mut usize).cast::<u8>());

        cache.sync_cached_object_bytes();

        assert_eq!(
            cache.previous_active_cached_class_idx(TOTAL_SIZE_CLASS - 1),
            Some(high_idx),
            "the bitmap scan still exposes the highest active class"
        );
        assert_eq!(
            cache.largest_retained_active_class_idx(),
            Some(low_idx),
            "aggregate pressure should be able to identify the real byte offender"
        );
    }

    #[test]
    fn thread_cache_maintains_active_cached_class_count_incrementally() {
        let mut cache = ThreadCache::new();
        let mut chunks = [0usize; 2];
        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);

        assert_eq!(cache.active_cached_class_count(), 0);
        assert_eq!(cache.cached_object_bytes, 0);

        let before = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.list[idx].free(chunks.as_mut_ptr().cast::<u8>());
        let after = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.account_class_cached_object_change(idx, before, after);
        assert_eq!(cache.active_cached_class_count(), 1);
        assert_eq!(cache.cached_object_bytes, rounded_size);
        assert!(cache.active_cached_class_bits.is_present(idx));

        let before = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.list[idx].free(unsafe { chunks.as_mut_ptr().add(1).cast::<u8>() });
        let after = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.account_class_cached_object_change(idx, before, after);
        assert_eq!(
            cache.active_cached_class_count(),
            1,
            "adding a second object to the same class must not inflate the dense-class counter"
        );
        assert_eq!(cache.cached_object_bytes, rounded_size * 2);

        let before = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.list[idx].clear_local_list();
        let after = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.account_class_cached_object_change(idx, before, after);
        assert_eq!(cache.active_cached_class_count(), 0);
        assert_eq!(cache.cached_object_bytes, 0);
        assert!(!cache.active_cached_class_bits.is_present(idx));
        assert_eq!(
            cache.previous_active_cached_class_idx(TOTAL_SIZE_CLASS - 1),
            None
        );
    }

    #[test]
    fn active_cached_class_bits_report_actual_transitions() {
        let mut bits = ActiveCachedClassBits::new();
        let idx = 1;

        assert!(bits.set_present(idx, true));
        assert!(!bits.set_present(idx, true));
        assert!(bits.is_present(idx));
        assert!(bits.set_present(idx, false));
        assert!(!bits.set_present(idx, false));
        assert!(!bits.is_present(idx));
        assert!(!bits.set_present(0, true));
        assert!(!bits.set_present(TOTAL_SIZE_CLASS, true));
    }

    #[test]
    fn thread_cache_active_count_uses_bitmap_transitions_for_stale_present_bit() {
        let mut cache = ThreadCache::new();
        let mut chunk = 0usize;
        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);

        // Model a defensive path that already marked the class present before
        // the byte accounting edge is replayed.  The active-class count must
        // follow the bitmap transition, not blindly add a second dense-budget
        // class for the same size class.
        assert!(cache.active_cached_class_bits.set_present(idx, true));
        cache.active_cached_classes = 1;

        let before = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.list[idx].free((&mut chunk as *mut usize).cast::<u8>());
        let after = ThreadCache::class_cached_object_bytes(idx, &cache.list[idx]);
        cache.account_class_cached_object_change(idx, before, after);

        assert_eq!(cache.active_cached_class_count(), 1);
        assert_eq!(cache.cached_object_bytes, rounded_size);
        assert!(cache.active_cached_class_bits.is_present(idx));
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_total_budget_returns_cross_class_surplus_to_real_slabs() {
        let _guard = fixed_heap_thread_cache_test_guard();

        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let mut allocated = alloc::vec::Vec::new();
        let mut naive_soft_bytes = 0usize;

        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            let soft_limit = thread_cache_object_limit(
                rounded_size,
                THREAD_CACHE_FLUSH_BYTES,
                THREAD_CACHE_FLUSH_OBJECTS_MAX,
            );
            naive_soft_bytes =
                naive_soft_bytes.saturating_add(soft_limit.saturating_mul(rounded_size));

            for _ in 0..soft_limit {
                let ptr = source.list[idx]
                    .allocate(idx, align_of::<usize>())
                    .expect("fixed-heap source cache should allocate real slab objects");
                allocated.push((idx, ptr));
            }
        }

        assert!(
            naive_soft_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "test must exceed the aggregate cap without relying on one overfull class"
        );

        for (idx, ptr) in allocated.drain(..) {
            let rounded_size = get_rounded_size_by_idx(idx);
            let layout = Layout::from_size_align(rounded_size, align_of::<usize>())
                .expect("size classes are word-aligned layouts");
            cache.deallocate(ptr, layout);
        }

        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "hot-path aggregate byte counter must match retained list and bump objects"
        );
        assert!(
            cache.cached_object_bytes <= THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "aggregate trim should return cross-class surplus to real slabs"
        );
        assert!(
            cache.cached_object_bytes < naive_soft_bytes,
            "aggregate cap must improve over independent per-class soft caps"
        );

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_dense_total_budget_trims_before_sparse_soft_cap() {
        let _guard = fixed_heap_thread_cache_test_guard();

        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let mut allocated = alloc::vec::Vec::new();
        let mut selected_classes = alloc::vec::Vec::new();

        for idx in (1..TOTAL_SIZE_CLASS).rev() {
            let rounded_size = get_rounded_size_by_idx(idx);
            if rounded_size != 0 {
                selected_classes.push((idx, rounded_size));
                if selected_classes.len() == THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD {
                    break;
                }
            }
        }
        assert_eq!(
            selected_classes.len(),
            THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD,
            "test needs enough active classes to exercise dense aggregate pressure"
        );

        let bytes_per_round = selected_classes
            .iter()
            .fold(0usize, |sum, &(_idx, rounded_size)| {
                sum.saturating_add(rounded_size)
            });
        let objects_per_class = THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES
            .saturating_add(64 * 1024)
            .saturating_add(bytes_per_round - 1)
            / bytes_per_round;

        for &(idx, _rounded_size) in &selected_classes {
            for _ in 0..objects_per_class {
                let ptr = source.list[idx]
                    .allocate(idx, align_of::<usize>())
                    .expect("fixed-heap source cache should allocate real slab objects");
                allocated.push((idx, ptr));
            }
        }

        let untrimmed_bytes = selected_classes
            .iter()
            .fold(0usize, |sum, &(_idx, rounded_size)| {
                sum.saturating_add(objects_per_class.saturating_mul(rounded_size))
            });
        assert!(
            untrimmed_bytes > THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES,
            "constructed dense pressure must cross the new dense soft cap"
        );
        assert!(
            untrimmed_bytes <= THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "constructed dense pressure must stay below the old sparse soft cap"
        );

        for (idx, ptr) in allocated.drain(..) {
            cache.list[idx].free(ptr.as_ptr());
        }
        cache.sync_cached_object_bytes();

        assert!(
            cache.active_cached_class_count() >= THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD,
            "all selected classes should be active before dense trimming"
        );
        assert_eq!(
            cache.cached_object_bytes, untrimmed_bytes,
            "manual real-slab setup should match the expected pre-trim dense footprint"
        );

        cache.trim_total_cached_object_bytes(None);

        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "dense aggregate trim must keep exact cached-object byte accounting"
        );
        assert!(
            cache.cached_object_bytes <= THREAD_CACHE_TOTAL_DENSE_TARGET_BYTES,
            "dense aggregate trim should reduce <=512KiB, which the old 2MiB cap would not have reached"
        );

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_total_budget_counts_bump_batches_under_pressure() {
        let _guard = fixed_heap_thread_cache_test_guard();

        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let bump_idx = (1..TOTAL_SIZE_CLASS)
            .rev()
            .find(|&idx| {
                let rounded_size = get_rounded_size_by_idx(idx);
                rounded_size != 0
                    && get_num_pages_by_idx(idx).saturating_mul(crate::PAGE_SIZE) / rounded_size > 1
            })
            .expect("fixed-heap table should include a class with multi-slot bump batches");
        let bump_size = get_rounded_size_by_idx(bump_idx);
        let bump_layout = Layout::from_size_align(bump_size, align_of::<usize>())
            .expect("size classes are word-aligned layouts");
        let (bump_user, held_before_bump) =
            allocate_until_bump_tail_for_test(&mut cache.list[bump_idx], bump_idx, bump_size);
        assert!(
            cache.list[bump_idx].bump_len() != 0,
            "test class must retain a real bump tail before aggregate trimming"
        );

        // Build enough real linked-list pressure in lower classes to force the
        // aggregate trimmer to visit the high-class bump tail.  We bypass the
        // public ThreadCache::deallocate wrapper here only to construct the
        // pre-trim state deterministically; all objects still come from real
        // fixed-heap slabs and are returned through the normal cleanup path.
        for idx in 1..bump_idx {
            let rounded_size = get_rounded_size_by_idx(idx);
            let soft_limit = thread_cache_object_limit(
                rounded_size,
                THREAD_CACHE_FLUSH_BYTES,
                THREAD_CACHE_FLUSH_OBJECTS_MAX,
            );
            for _ in 0..soft_limit {
                let ptr = source.list[idx]
                    .allocate(idx, align_of::<usize>())
                    .expect("source cache should allocate real slab objects");
                cache.list[idx].free(ptr.as_ptr());
            }
        }

        cache.sync_cached_object_bytes();
        assert!(
            cache.cached_object_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "constructed list+bump pressure must exceed the aggregate cap"
        );

        cache.trim_total_cached_object_bytes(None);

        assert_eq!(
            cache.list[bump_idx].bump_len(),
            0,
            "aggregate trimming must drain bump tails instead of counting only linked lists"
        );
        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "aggregate byte counter must match retained list+bump state after trim"
        );
        assert!(
            cache.cached_object_bytes <= THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "aggregate trim should bring retained objects back under the soft cap"
        );

        cache.deallocate(bump_user, bump_layout);
        for ptr in held_before_bump {
            cache.deallocate(ptr, bump_layout);
        }
        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn thread_cache_total_budget_converges_below_target_after_multi_class_pressure() {
        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let mut normal_targets = alloc::vec::Vec::new();
        let mut normal_target_bytes = 0usize;
        let mut initial_bytes = 0usize;

        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            if rounded_size < 1024 || rounded_size >= crate::PAGE_SIZE {
                continue;
            }

            let normal_target =
                thread_cache_target_keep_length(usize::MAX / rounded_size, rounded_size);
            let fill_count = normal_target
                .checked_mul(3)
                .and_then(|count| count.checked_add(1))
                .expect("test fill count should stay small for sub-page classes");
            normal_targets.push((idx, rounded_size, normal_target));
            normal_target_bytes =
                normal_target_bytes.saturating_add(normal_target.saturating_mul(rounded_size));
            initial_bytes = initial_bytes.saturating_add(fill_count.saturating_mul(rounded_size));

            for _ in 0..fill_count {
                let ptr = source.list[idx]
                    .allocate(idx, align_of::<usize>())
                    .expect("source cache should allocate real slab objects");
                cache.list[idx].free(ptr.as_ptr());
            }
        }

        assert!(
            normal_targets.len() > 1,
            "test needs multiple sub-page large-ish classes"
        );
        assert!(
            normal_target_bytes > THREAD_CACHE_TOTAL_TARGET_BYTES,
            "first-phase per-class hot targets must still exceed the aggregate target"
        );
        assert!(
            initial_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "constructed multi-class pressure must cross the aggregate flush cap"
        );

        cache.sync_cached_object_bytes();
        cache.trim_total_cached_object_bytes(None);

        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "aggregate byte counter must match retained real objects after trim"
        );
        assert!(
            cache.cached_object_bytes <= THREAD_CACHE_TOTAL_TARGET_BYTES,
            "aggregate trim should converge at or below the target"
        );
        assert!(
            normal_targets
                .iter()
                .any(|&(idx, _rounded_size, normal_target)| {
                    cache.list[idx].list.length < normal_target
                }),
            "second phase must trim at least one class below its normal per-class hot target"
        );

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(not(feature = "fixed_heap"))]
    #[test]
    fn thread_cache_aggregate_pressure_prefers_retained_bytes_over_high_index() {
        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let mut selected_classes = alloc::vec::Vec::new();

        for idx in (1..TOTAL_SIZE_CLASS).rev().take(10) {
            let rounded_size = get_rounded_size_by_idx(idx);
            let normal_target =
                thread_cache_target_keep_length(usize::MAX / rounded_size, rounded_size);
            selected_classes.push((idx, rounded_size, normal_target));
        }
        assert!(
            selected_classes.len() >= THREAD_CACHE_TOTAL_DENSE_CLASS_THRESHOLD,
            "test needs dense aggregate pressure"
        );

        let high_idx = TOTAL_SIZE_CLASS - 1;
        let (high_size, _high_normal_target) = selected_classes
            .iter()
            .find_map(|&(idx, rounded_size, normal_target)| {
                (idx == high_idx).then_some((rounded_size, normal_target))
            })
            .expect("highest size class should be selected");
        let high_hot_prefix_len = 1usize;
        let high_hot_prefix_bytes = high_size.saturating_mul(high_hot_prefix_len);
        let mut offender = None;
        let mut offender_bytes = 0usize;
        for &(idx, rounded_size, normal_target) in &selected_classes {
            let retained_bytes = rounded_size.saturating_mul(normal_target);
            if idx != high_idx
                && retained_bytes > high_hot_prefix_bytes
                && retained_bytes > offender_bytes
            {
                offender = Some((idx, rounded_size, normal_target));
                offender_bytes = retained_bytes;
            }
        }
        let (offender_idx, offender_size, offender_normal_target) = offender
            .expect("a lower class should retain more bytes than the high-index hot prefix");

        let (_flush_bytes, dense_target_bytes) =
            thread_cache_total_pressure_budget(selected_classes.len());
        assert_eq!(
            dense_target_bytes, THREAD_CACHE_TOTAL_DENSE_TARGET_BYTES,
            "selected classes should use the dense aggregate target"
        );
        let normal_target_bytes =
            selected_classes
                .iter()
                .fold(0usize, |sum, &(idx, size, target)| {
                    let retained = if idx == high_idx {
                        high_hot_prefix_len
                    } else {
                        target
                    };
                    sum.saturating_add(size.saturating_mul(retained))
                });
        assert!(
            normal_target_bytes > dense_target_bytes,
            "normal per-class hot targets plus the high hot prefix must still exceed the dense aggregate target"
        );

        let bytes_without_offender =
            normal_target_bytes.saturating_sub(offender_size * offender_normal_target);
        let offender_count = THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES
            .saturating_sub(bytes_without_offender)
            .saturating_add(offender_size - 1)
            / offender_size
            + 4;

        for &(idx, rounded_size, normal_target) in &selected_classes {
            let count = if idx == offender_idx {
                core::cmp::max(offender_count, normal_target + 1)
            } else if idx == high_idx {
                high_hot_prefix_len
            } else {
                // Give the non-offender classes exactly one cold object above
                // their normal target.  The first trimming phase can return
                // that cold suffix, but it cannot solve aggregate pressure by
                // repeatedly shaving these classes below their hot target.
                normal_target + 1
            };
            for _ in 0..count {
                let ptr = source.list[idx]
                    .allocate(idx, align_of::<usize>())
                    .expect("source cache should allocate real slab objects");
                cache.list[idx].free(ptr.as_ptr());
            }
            assert!(
                count > 0,
                "class {} ({} bytes) should contribute real cached objects",
                idx,
                rounded_size
            );
        }

        cache.sync_cached_object_bytes();
        assert_eq!(cache.active_cached_class_count(), selected_classes.len());
        assert!(
            cache.cached_object_bytes > THREAD_CACHE_TOTAL_DENSE_FLUSH_BYTES,
            "constructed pressure must cross the dense aggregate soft cap"
        );
        assert_eq!(
            cache.list[high_idx].list.length, high_hot_prefix_len,
            "the high-index class starts as a one-object hot prefix; only index-ordered aggregate trimming would touch it"
        );
        assert_eq!(
            cache.largest_retained_active_class_idx(),
            Some(offender_idx),
            "the byte offender should be visible before aggregate trimming"
        );

        cache.trim_total_cached_object_bytes(None);

        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "byte-first aggregate trim must keep accounting exact"
        );
        assert!(
            cache.cached_object_bytes <= dense_target_bytes,
            "aggregate-pressure phase should converge to the dense target"
        );
        assert!(
            cache.list[offender_idx].list.length < offender_normal_target,
            "the retained-byte offender should be trimmed below its normal hot target before unrelated classes"
        );
        assert_eq!(
            cache.list[high_idx].list.length, high_hot_prefix_len,
            "a high-index hot prefix below the byte offender should not be trimmed merely because of class order"
        );

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_total_budget_converges_below_target_after_multi_class_pressure() {
        let _guard = fixed_heap_thread_cache_test_guard();

        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let mut normal_targets = alloc::vec::Vec::new();
        let mut normal_target_bytes = 0usize;
        let mut initial_bytes = 0usize;
        let mut largeish_classes = 0usize;

        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded_size = get_rounded_size_by_idx(idx);
            if rounded_size >= 1024 && rounded_size < crate::PAGE_SIZE {
                largeish_classes += 1;
            }

            let normal_target =
                thread_cache_target_keep_length(usize::MAX / rounded_size, rounded_size);
            let fill_count = normal_target
                .checked_mul(3)
                .and_then(|count| count.checked_add(1))
                .expect("test fill count should stay small for fixed-heap classes");
            normal_targets.push((idx, rounded_size, normal_target));
            normal_target_bytes =
                normal_target_bytes.saturating_add(normal_target.saturating_mul(rounded_size));
            initial_bytes = initial_bytes.saturating_add(fill_count.saturating_mul(rounded_size));

            for _ in 0..fill_count {
                let ptr = source.list[idx]
                    .allocate(idx, align_of::<usize>())
                    .expect("fixed-heap source cache should allocate real slab objects");
                cache.list[idx].free(ptr.as_ptr());
            }
        }

        assert!(
            largeish_classes >= 3,
            "fixed-heap table should include multiple sub-page large-ish classes"
        );
        assert!(
            initial_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "constructed multi-class pressure must cross the aggregate flush cap"
        );

        cache.sync_cached_object_bytes();
        cache.trim_total_cached_object_bytes(None);

        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "aggregate byte counter must match retained real objects after trim"
        );
        let (_flush_bytes, selected_target_bytes) =
            thread_cache_total_pressure_budget(cache.active_cached_class_count());
        assert!(
            cache.cached_object_bytes <= selected_target_bytes,
            "aggregate trim should converge at or below the selected target"
        );

        let trimmed_below_normal_target =
            normal_targets
                .iter()
                .any(|&(idx, _rounded_size, normal_target)| {
                    cache.list[idx].list.length < normal_target
                });
        if normal_target_bytes > selected_target_bytes {
            assert!(
                trimmed_below_normal_target,
                "when per-class hot targets exceed the selected aggregate target, the second phase must trim below at least one normal target"
            );
        } else {
            assert!(
                !trimmed_below_normal_target,
                "this fixed-heap table's normal per-class target sum is already below the selected aggregate target"
            );
        }

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_total_budget_prefers_recent_offender_before_largest_sweep() {
        let _guard = fixed_heap_thread_cache_test_guard();

        let preferred_idx = TOTAL_SIZE_CLASS - 2;
        let higher_idx = TOTAL_SIZE_CLASS - 1;
        let preferred_size = get_rounded_size_by_idx(preferred_idx);
        let higher_size = get_rounded_size_by_idx(higher_idx);
        assert!(preferred_size < higher_size);

        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let higher_count = 64usize;
        let higher_target = thread_cache_target_keep_length(higher_count, higher_size);
        assert!(
            higher_target < higher_count,
            "higher class must be large enough that the old largest-first sweep would trim it"
        );

        let preferred_bytes_needed =
            THREAD_CACHE_TOTAL_FLUSH_BYTES.saturating_sub(higher_count.saturating_mul(higher_size));
        let preferred_count = preferred_bytes_needed
            .saturating_add(preferred_size - 1)
            .checked_div(preferred_size)
            .expect("preferred test size class must be non-zero")
            .saturating_add(2);
        let preferred_target = thread_cache_target_keep_length(preferred_count, preferred_size);

        for _ in 0..preferred_count {
            let ptr = source.list[preferred_idx]
                .allocate(preferred_idx, align_of::<usize>())
                .expect("source cache should allocate real preferred-class slab objects");
            cache.list[preferred_idx].free(ptr.as_ptr());
        }
        for _ in 0..higher_count {
            let ptr = source.list[higher_idx]
                .allocate(higher_idx, align_of::<usize>())
                .expect("source cache should allocate real higher-class slab objects");
            cache.list[higher_idx].free(ptr.as_ptr());
        }

        cache.sync_cached_object_bytes();
        assert!(
            cache.cached_object_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "constructed pressure must cross the aggregate soft cap"
        );
        assert!(
            preferred_target
                .saturating_mul(preferred_size)
                .saturating_add(higher_count.saturating_mul(higher_size))
                <= THREAD_CACHE_TOTAL_TARGET_BYTES,
            "preferred class alone must be able to restore the aggregate target"
        );

        cache.trim_total_cached_object_bytes(Some(preferred_idx));

        assert_eq!(
            cache.list[preferred_idx].list.length, preferred_target,
            "aggregate trimming should reduce the just-grown preferred class first"
        );
        assert_eq!(
            cache.list[higher_idx].list.length, higher_count,
            "preferred trimming should avoid flushing an unrelated larger class when it restores budget"
        );
        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "aggregate byte counter must match retained real objects after preferred trim"
        );
        assert!(
            cache.cached_object_bytes <= THREAD_CACHE_TOTAL_TARGET_BYTES,
            "preferred class trim should restore the aggregate target without a full sweep"
        );

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[cfg(feature = "fixed_heap")]
    #[test]
    fn thread_cache_total_budget_prefers_largest_retained_class_before_index_sweep() {
        let _guard = fixed_heap_thread_cache_test_guard();

        let lower_idx = TOTAL_SIZE_CLASS - 2;
        let higher_idx = TOTAL_SIZE_CLASS - 1;
        let lower_size = get_rounded_size_by_idx(lower_idx);
        let higher_size = get_rounded_size_by_idx(higher_idx);
        assert!(lower_size < higher_size);

        let mut source = ThreadCache::new();
        let mut cache = ThreadCache::new();
        let higher_count = 64usize;
        let higher_target = thread_cache_target_keep_length(higher_count, higher_size);
        assert!(
            higher_target < higher_count,
            "higher class must be trim-capable so the old index sweep would touch it"
        );

        let lower_bytes_needed =
            THREAD_CACHE_TOTAL_FLUSH_BYTES.saturating_sub(higher_count.saturating_mul(higher_size));
        let lower_count = lower_bytes_needed
            .saturating_add(lower_size - 1)
            .checked_div(lower_size)
            .expect("lower test size class must be non-zero")
            .saturating_add(2);
        let lower_target = thread_cache_target_keep_length(lower_count, lower_size);
        assert!(
            lower_target < lower_count,
            "lower class must be trim-capable under the normal hot target"
        );
        assert!(
            lower_count.saturating_mul(lower_size) > higher_count.saturating_mul(higher_size),
            "lower class must retain more bytes despite having a lower class index"
        );
        assert!(
            lower_target
                .saturating_mul(lower_size)
                .saturating_add(higher_count.saturating_mul(higher_size))
                <= THREAD_CACHE_TOTAL_TARGET_BYTES,
            "trimming the largest retained class alone should restore the aggregate target"
        );

        for _ in 0..lower_count {
            let ptr = source.list[lower_idx]
                .allocate(lower_idx, align_of::<usize>())
                .expect("source cache should allocate real lower-class slab objects");
            cache.list[lower_idx].free(ptr.as_ptr());
        }
        for _ in 0..higher_count {
            let ptr = source.list[higher_idx]
                .allocate(higher_idx, align_of::<usize>())
                .expect("source cache should allocate real higher-class slab objects");
            cache.list[higher_idx].free(ptr.as_ptr());
        }

        cache.sync_cached_object_bytes();
        assert!(
            cache.cached_object_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES,
            "constructed pressure must cross the aggregate soft cap"
        );
        assert_eq!(
            cache.largest_retained_active_class_idx(),
            Some(lower_idx),
            "largest-retained selection should prefer bytes over class index"
        );

        cache.trim_total_cached_object_bytes(None);

        assert_eq!(
            cache.list[lower_idx].list.length, lower_target,
            "aggregate trimming should reduce the largest retained class first"
        );
        assert_eq!(
            cache.list[higher_idx].list.length, higher_count,
            "byte-first trimming should leave the unrelated higher-index hot prefix alone"
        );
        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes(),
            "aggregate byte counter must match retained real objects after byte-first trim"
        );
        assert!(
            cache.cached_object_bytes <= THREAD_CACHE_TOTAL_TARGET_BYTES,
            "byte-first trim should restore the aggregate target without an index sweep"
        );

        source.cleanup_cache_unchecked();
        cache.cleanup_cache_unchecked();
    }

    #[test]
    fn thread_cache_should_flush_handles_huge_values_without_overflow() {
        assert!(thread_cache_should_flush(usize::MAX, usize::MAX));
    }

    #[test]
    fn thread_cache_blocking_flush_waits_until_hard_limit() {
        let rounded_size = 8;
        let soft_limit = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );
        let hard_limit = soft_limit * THREAD_CACHE_BLOCKING_FLUSH_MULTIPLIER;

        assert!(thread_cache_should_flush(soft_limit + 1, rounded_size));
        assert!(!thread_cache_should_blocking_flush(
            soft_limit + 1,
            rounded_size
        ));
        assert!(!thread_cache_should_blocking_flush(
            hard_limit,
            rounded_size
        ));
        assert!(thread_cache_should_blocking_flush(
            hard_limit + 1,
            rounded_size
        ));
    }

    #[test]
    fn thread_cache_blocking_flush_caps_oversized_classes_after_two_objects() {
        let rounded_size = THREAD_CACHE_FLUSH_BYTES * 2;

        assert_eq!(
            thread_cache_object_limit(
                rounded_size,
                THREAD_CACHE_FLUSH_BYTES,
                THREAD_CACHE_FLUSH_OBJECTS_MAX,
            ),
            1
        );
        assert!(!thread_cache_should_blocking_flush(2, rounded_size));
        assert!(thread_cache_should_blocking_flush(3, rounded_size));
    }

    #[test]
    fn thread_cache_soft_flush_attempt_due_uses_geometric_retries() {
        let rounded_size = 8;
        let soft_limit = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );

        assert!(thread_cache_soft_flush_attempt_due(
            soft_limit + 1,
            rounded_size
        ));
        assert!(thread_cache_soft_flush_attempt_due(
            soft_limit + 2,
            rounded_size
        ));
        assert!(
            !thread_cache_soft_flush_attempt_due(soft_limit + 3, rounded_size),
            "soft retries should skip non-power-of-two overflow lengths to avoid repeated detach/restore churn"
        );
        assert!(thread_cache_soft_flush_attempt_due(
            soft_limit + 4,
            rounded_size
        ));
        assert!(
            !thread_cache_soft_flush_attempt_due(soft_limit + 5, rounded_size),
            "geometric retry schedule should not add per-thread-cache metadata"
        );
    }

    #[test]
    fn thread_cache_soft_flush_attempt_due_does_not_skip_hard_cap() {
        let rounded_size = 8;
        let soft_limit = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );
        let hard_limit = soft_limit * THREAD_CACHE_BLOCKING_FLUSH_MULTIPLIER;

        assert!(thread_cache_should_blocking_flush(
            hard_limit + 1,
            rounded_size
        ));
        assert!(thread_cache_soft_flush_attempt_due(
            hard_limit + 1,
            rounded_size
        ));
    }

    #[cfg(all(feature = "fixed_heap", feature = "stats"))]
    #[test]
    fn thread_cache_aggregate_trim_deduplicates_busy_class_in_one_pass() {
        let _guard = fixed_heap_thread_cache_test_guard();
        thread_cache_flush_stats_reset();

        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        let count = THREAD_CACHE_TOTAL_FLUSH_BYTES
            .checked_div(rounded_size)
            .expect("test class must have non-zero size")
            .saturating_add(1);
        let mut chunks = alloc::vec::Vec::<usize>::new();
        chunks.resize(count, 0);

        let mut cache = ThreadCache::new();
        for chunk in chunks.iter_mut() {
            cache.list[idx].free((chunk as *mut usize).cast::<u8>());
        }
        cache.sync_cached_object_bytes();

        assert_eq!(cache.active_cached_class_count(), 1);
        assert!(cache.cached_object_bytes > THREAD_CACHE_TOTAL_FLUSH_BYTES);
        assert!(
            cache.cached_object_bytes
                <= thread_cache_total_blocking_flush_limit(cache.active_cached_class_count()),
            "test should exercise the nonblocking aggregate trim path"
        );

        let zone = crate::zone::try_global_zone().expect("fixed heap zone must be initialized");
        let _busy_slab = zone
            .test_lock_slab_for_contention(idx)
            .expect("test size class must exist");

        cache.trim_total_cached_object_bytes(Some(idx));

        let snapshot = thread_cache_flush_stats_snapshot();
        assert_eq!(
            snapshot.try_lock_attempts, 1,
            "one aggregate trim pass should not retry the same busy class through preferred/largest/sweep phases"
        );
        assert_eq!(snapshot.busy_no_transfer, 1);
        assert_eq!(snapshot.restored_after_busy, 1);
        assert_eq!(
            cache.cached_object_bytes,
            cache.recompute_cached_object_bytes()
        );
        assert_eq!(cache.active_cached_class_count(), 1);
        assert!(cache.active_cached_class_bits.is_present(idx));
    }

    #[cfg(all(feature = "fixed_heap", feature = "stats"))]
    #[test]
    fn thread_cache_flush_stats_record_busy_restore_and_geometric_skip() {
        let _guard = fixed_heap_thread_cache_test_guard();
        thread_cache_flush_stats_reset();

        let idx = 1;
        let rounded_size = get_rounded_size_by_idx(idx);
        let soft_limit = thread_cache_object_limit(
            rounded_size,
            THREAD_CACHE_FLUSH_BYTES,
            THREAD_CACHE_FLUSH_OBJECTS_MAX,
        );
        let mut cache = ThreadCacheUnit::new();
        let mut chunks = [0usize; THREAD_CACHE_FLUSH_OBJECTS_MAX + 3];
        for chunk in chunks.iter_mut().take(soft_limit) {
            cache.free((chunk as *mut usize).cast::<u8>());
        }

        let zone = crate::zone::try_global_zone().expect("fixed heap zone must be initialized");
        let _busy_slab = zone
            .test_lock_slab_for_contention(idx)
            .expect("test size class must exist");

        for offset in 0..3 {
            let ptr = unsafe { chunks.as_mut_ptr().add(soft_limit + offset).cast::<u8>() };
            cache.deallocate(
                idx,
                NonNull::new(ptr).expect("test chunk is non-null"),
                rounded_size,
            );
        }

        let snapshot = thread_cache_flush_stats_snapshot();
        assert_eq!(snapshot.try_lock_attempts, 2);
        assert_eq!(snapshot.busy_no_transfer, 2);
        assert_eq!(snapshot.restored_after_busy, 2);
        assert_eq!(snapshot.geometric_soft_skips, 1);
        assert_eq!(snapshot.hard_fallback_attempts, 0);
        assert_eq!(snapshot.backend_errors, 0);
        assert_eq!(snapshot.uncertain_suffix_drops, 0);
    }
}

#[cfg(feature = "fixed_heap")]
pub mod fixed_tcache {
    use crate::cache::ThreadCache;
    use core::ptr::null_mut;
    use core::sync::atomic::{AtomicPtr, Ordering};
    use spin::Mutex;

    /// Fixed-heap builds intentionally use one process-wide cache because they
    /// cannot rely on a hosted TLS allocator. Publish the root atomically and
    /// keep all mutable access inside one lock guard: an AtomicPtr alone would
    /// still allow multiple threads to manufacture aliased `&mut ThreadCache`
    /// values and corrupt the intrusive free lists.
    pub static GLOBAL_TCACHE_PTR: AtomicPtr<ThreadCache> = AtomicPtr::new(null_mut());
    static GLOBAL_TCACHE_LOCK: Mutex<()> = Mutex::new(());

    /// Publish a fully initialized fixed-heap cache root.
    ///
    /// # Safety
    ///
    /// `ptr` must remain valid and uniquely owned by this module for the rest
    /// of the allocator lifetime. It must point to an initialized
    /// `ThreadCache` before this function is called.
    pub(crate) unsafe fn install_fixed_tcache(ptr: *mut ThreadCache) {
        GLOBAL_TCACHE_PTR.store(ptr, Ordering::Release);
    }

    #[inline]
    pub(crate) fn fixed_tcache_initialized() -> bool {
        !GLOBAL_TCACHE_PTR.load(Ordering::Acquire).is_null()
    }

    #[inline]
    pub(crate) fn with_fixed_tcache_mut<R>(f: impl FnOnce(&mut ThreadCache) -> R) -> Option<R> {
        let _guard = GLOBAL_TCACHE_LOCK.lock();
        let ptr = GLOBAL_TCACHE_PTR.load(Ordering::Acquire);
        unsafe { ptr.as_mut().map(f) }
    }
}
#[cfg(feature = "fixed_heap")]
pub use fixed_tcache as Fixed_TCache;
#[cfg(feature = "fixed_heap")]
pub use fixed_tcache::GLOBAL_TCACHE_PTR as GlobalTcache_ptr;
#[cfg(feature = "fixed_heap")]
pub use fixed_tcache::*;

#[cfg(not(feature = "fixed_heap"))]
pub fn thread_cache_footprint_snapshot() -> ThreadCacheFootprintSnapshot {
    (&(*GlobalTcache)).footprint_snapshot()
}

#[cfg(feature = "fixed_heap")]
pub fn thread_cache_footprint_snapshot() -> ThreadCacheFootprintSnapshot {
    fixed_tcache::with_fixed_tcache_mut(|cache| cache.footprint_snapshot()).unwrap_or_else(|| {
        ThreadCacheFootprintSnapshot {
            cache_initialized: false,
            ..ThreadCacheFootprintSnapshot::default()
        }
    })
}

// #[cfg(test)]
// mod tests {
//     use super::*;
//
//     struct B;
//     impl Drop for B {
//         fn drop(&mut self) {
//             println!("dropin");
//         }
//     }
//
//     #[cfg(target_os = "linux")]
//     #[test]
//     fn tls_drop_test() {
//         use core::sync::atomic::{AtomicI32, Ordering};
//         extern crate std;
//         use static_init::dynamic;
//         use std::thread::spawn;
//
//         #[dynamic(drop)]
//         #[thread_local]
//         static B1: B = B;
//
//         std::thread::spawn(|| {
//             let _ = &*B1;
//         })
//         .join()
//         .unwrap();
//     }
//
//     #[cfg(target_os = "linux")]
//     #[test]
//     fn static_init_tcache_drop_test() {
//         use core::sync::atomic::{AtomicI32, Ordering};
//         extern crate std;
//         use std::thread::spawn;
//
//         spawn(move || {
//             let _x = get_thread_cache();
//         })
//         .join()
//         .unwrap();
//     }
// }
