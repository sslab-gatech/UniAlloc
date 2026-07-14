//! Hosted page-run routing between exact free-list reuse and bitmap arenas.
//!
//! The two backends own disjoint OS mappings. A free-list deallocation that
//! bridges both neighbours publishes a cold generation signal. Radix-tree lock
//! contention publishes a second signal. Each thread that observes a burst of
//! bridge merges or any real contention leases a bounded number of subsequent
//! page runs to the bitmap backend, then probes the free-list path again.

use crate::freelist::FREELIST;
use crate::hosted_bitmap_alloc::HOSTED_PAGE_RUN_BITMAP;
use core::alloc::Layout;
use core::ptr::null_mut;
use core::sync::atomic::{AtomicUsize, Ordering};

const FREELIST_BRIDGE_THRESHOLD: usize = 4;
const BRIDGE_ROUTE_MIN_PAGES: usize = 16;
const BITMAP_LEASE_ALLOCATIONS: usize = 64 * 1024;

static FREELIST_ROUTING_GENERATION: AtomicUsize = AtomicUsize::new(0);
static FREELIST_CONTENTION_GENERATION: AtomicUsize = AtomicUsize::new(0);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct RouteDecision {
    use_bitmap: bool,
    bridge_activated: bool,
    contention_activated: bool,
}

#[derive(Clone, Copy)]
struct AdaptiveThreadState {
    initialized: bool,
    observed_routing_generation: usize,
    observed_contention_generation: usize,
    bitmap_lease_remaining: usize,
}

impl AdaptiveThreadState {
    const fn new() -> Self {
        Self {
            initialized: false,
            observed_routing_generation: 0,
            observed_contention_generation: 0,
            bitmap_lease_remaining: 0,
        }
    }

    #[inline]
    fn consume_active_lease(&mut self, refresh: bool, lease_allocations: usize) -> bool {
        if self.bitmap_lease_remaining == 0 {
            return false;
        }
        // Large page runs are the bridge/coalescing demand that justified the
        // route, so keep that phase on one backend. Small exact runs consume the
        // remaining lease and return to the free list promptly after a phase
        // change.
        if refresh {
            self.bitmap_lease_remaining = lease_allocations;
        }
        self.bitmap_lease_remaining -= 1;
        true
    }

    #[inline]
    fn choose_bitmap(
        &mut self,
        routing_generation: usize,
        contention_generation: usize,
        bridge_eligible: bool,
        bridge_threshold: usize,
        lease_allocations: usize,
    ) -> RouteDecision {
        if !self.initialized {
            self.initialized = true;
            self.observed_routing_generation = routing_generation;
            self.observed_contention_generation = contention_generation;
            return RouteDecision {
                use_bitmap: false,
                bridge_activated: false,
                contention_activated: false,
            };
        }

        if self.consume_active_lease(bridge_eligible, lease_allocations) {
            return RouteDecision {
                use_bitmap: true,
                bridge_activated: false,
                contention_activated: false,
            };
        }

        let routing_delta = routing_generation.wrapping_sub(self.observed_routing_generation);
        if routing_delta == 0 {
            return RouteDecision {
                use_bitmap: false,
                bridge_activated: false,
                contention_activated: false,
            };
        }
        let contention_delta =
            contention_generation.wrapping_sub(self.observed_contention_generation);
        self.observed_routing_generation = routing_generation;
        self.observed_contention_generation = contention_generation;
        let bridge_delta = routing_delta.saturating_sub(contention_delta);

        // A bridge burst distinguishes coalesce/refill phases from sparse
        // exact-size churn. Real lock contention is already a strong signal.
        let bridge_activated = bridge_eligible && bridge_delta >= bridge_threshold;
        let contention_activated = contention_delta != 0;
        if bridge_activated || contention_activated {
            self.bitmap_lease_remaining = lease_allocations;
        }
        let use_bitmap = self.bitmap_lease_remaining != 0;
        if use_bitmap {
            self.bitmap_lease_remaining -= 1;
        }
        RouteDecision {
            use_bitmap,
            bridge_activated,
            contention_activated,
        }
    }
}

#[cfg(not(unialloc_target_arm64e))]
#[thread_local]
static mut ADAPTIVE_THREAD_STATE: AdaptiveThreadState = AdaptiveThreadState::new();

pub(crate) fn note_freelist_bridge_merge() {
    FREELIST_ROUTING_GENERATION.fetch_add(1, Ordering::Release);
    #[cfg(feature = "stats")]
    ADAPTIVE_STATS
        .bridge_signals
        .fetch_add(1, Ordering::Relaxed);
}

pub(crate) fn note_freelist_contention() {
    FREELIST_CONTENTION_GENERATION.fetch_add(1, Ordering::Relaxed);
    // Publish the shared routing generation after the contention generation so
    // an acquiring route observer sees both changes.
    FREELIST_ROUTING_GENERATION.fetch_add(1, Ordering::Release);
    #[cfg(feature = "stats")]
    ADAPTIVE_STATS
        .contention_signals
        .fetch_add(1, Ordering::Relaxed);
}

#[cfg(not(unialloc_target_arm64e))]
#[inline]
fn choose_bitmap(layout: Layout) -> bool {
    let state = unsafe {
        core::ptr::addr_of_mut!(ADAPTIVE_THREAD_STATE)
            .as_mut()
            .expect("thread-local adaptive state")
    };
    if state.initialized && state.bitmap_lease_remaining != 0 {
        let bridge_eligible = layout.size() >= BRIDGE_ROUTE_MIN_PAGES * crate::PAGE_SIZE;
        let consumed = state.consume_active_lease(bridge_eligible, BITMAP_LEASE_ALLOCATIONS);
        debug_assert!(consumed);
        return true;
    }

    let routing_generation = FREELIST_ROUTING_GENERATION.load(Ordering::Acquire);
    if state.initialized && routing_generation == state.observed_routing_generation {
        return false;
    }
    let contention_generation = FREELIST_CONTENTION_GENERATION.load(Ordering::Relaxed);
    let bridge_eligible = layout.size() >= BRIDGE_ROUTE_MIN_PAGES * crate::PAGE_SIZE;
    let decision = state.choose_bitmap(
        routing_generation,
        contention_generation,
        bridge_eligible,
        FREELIST_BRIDGE_THRESHOLD,
        BITMAP_LEASE_ALLOCATIONS,
    );
    #[cfg(feature = "stats")]
    {
        if decision.bridge_activated {
            ADAPTIVE_STATS
                .bridge_activations
                .fetch_add(1, Ordering::Relaxed);
        }
        if decision.contention_activated {
            ADAPTIVE_STATS
                .contention_activations
                .fetch_add(1, Ordering::Relaxed);
        }
    }
    decision.use_bitmap
}

#[cfg(unialloc_target_arm64e)]
#[inline]
fn choose_bitmap(_layout: Layout) -> bool {
    false
}

#[doc(hidden)]
#[inline]
pub fn route_probe_for_bench(layout: Layout) -> bool {
    choose_bitmap(layout)
}

#[inline]
unsafe fn allocate_freelist(layout: Layout) -> *mut u8 {
    if layout.align() > crate::PAGE_SIZE {
        if layout.align() % crate::PAGE_SIZE != 0 {
            return null_mut();
        }
        FREELIST
            .alloc_aligned(layout.size(), layout.align())
            .unwrap_or(null_mut())
    } else {
        FREELIST.alloc(layout.size()).unwrap_or(null_mut())
    }
}

pub(crate) unsafe fn allocate_layout(layout: Layout) -> *mut u8 {
    if choose_bitmap(layout) {
        if let Ok(ptr) = HOSTED_PAGE_RUN_BITMAP.allocate_pooled_layout(layout) {
            #[cfg(feature = "stats")]
            ADAPTIVE_STATS
                .bitmap_allocations
                .fetch_add(1, Ordering::Relaxed);
            return ptr;
        }
        #[cfg(feature = "stats")]
        ADAPTIVE_STATS
            .bitmap_fallbacks
            .fetch_add(1, Ordering::Relaxed);
    }
    let ptr = allocate_freelist(layout);
    #[cfg(feature = "stats")]
    ADAPTIVE_STATS
        .freelist_allocations
        .fetch_add(1, Ordering::Relaxed);
    ptr
}

pub(crate) unsafe fn deallocate_layout(ptr: *mut u8, layout: Layout) {
    match HOSTED_PAGE_RUN_BITMAP.try_deallocate_owned(ptr, layout) {
        Ok(true) => {
            #[cfg(feature = "stats")]
            ADAPTIVE_STATS
                .owned_deallocations
                .fetch_add(1, Ordering::Relaxed);
        }
        Ok(false) => {
            #[cfg(feature = "stats")]
            ADAPTIVE_STATS
                .freelist_deallocations
                .fetch_add(1, Ordering::Relaxed);
            FREELIST.free(ptr, layout.size());
        }
        Err(error) => {
            debug_assert!(false, "adaptive bitmap deallocation rejected: {:?}", error);
        }
    }
}

#[cfg(feature = "stats")]
struct AdaptiveStats {
    freelist_allocations: AtomicUsize,
    bitmap_allocations: AtomicUsize,
    bitmap_fallbacks: AtomicUsize,
    freelist_deallocations: AtomicUsize,
    owned_deallocations: AtomicUsize,
    bridge_signals: AtomicUsize,
    bridge_activations: AtomicUsize,
    contention_signals: AtomicUsize,
    contention_activations: AtomicUsize,
}

#[cfg(feature = "stats")]
static ADAPTIVE_STATS: AdaptiveStats = AdaptiveStats {
    freelist_allocations: AtomicUsize::new(0),
    bitmap_allocations: AtomicUsize::new(0),
    bitmap_fallbacks: AtomicUsize::new(0),
    freelist_deallocations: AtomicUsize::new(0),
    owned_deallocations: AtomicUsize::new(0),
    bridge_signals: AtomicUsize::new(0),
    bridge_activations: AtomicUsize::new(0),
    contention_signals: AtomicUsize::new(0),
    contention_activations: AtomicUsize::new(0),
};

#[cfg(feature = "stats")]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct AdaptivePageRunStats {
    pub freelist_allocations: usize,
    pub bitmap_allocations: usize,
    pub bitmap_fallbacks: usize,
    pub freelist_deallocations: usize,
    pub owned_deallocations: usize,
    pub bridge_signals: usize,
    pub bridge_activations: usize,
    pub contention_signals: usize,
    pub contention_activations: usize,
}

#[cfg(feature = "stats")]
pub fn stats_snapshot() -> AdaptivePageRunStats {
    AdaptivePageRunStats {
        freelist_allocations: ADAPTIVE_STATS.freelist_allocations.load(Ordering::Relaxed),
        bitmap_allocations: ADAPTIVE_STATS.bitmap_allocations.load(Ordering::Relaxed),
        bitmap_fallbacks: ADAPTIVE_STATS.bitmap_fallbacks.load(Ordering::Relaxed),
        freelist_deallocations: ADAPTIVE_STATS
            .freelist_deallocations
            .load(Ordering::Relaxed),
        owned_deallocations: ADAPTIVE_STATS.owned_deallocations.load(Ordering::Relaxed),
        bridge_signals: ADAPTIVE_STATS.bridge_signals.load(Ordering::Relaxed),
        bridge_activations: ADAPTIVE_STATS.bridge_activations.load(Ordering::Relaxed),
        contention_signals: ADAPTIVE_STATS.contention_signals.load(Ordering::Relaxed),
        contention_activations: ADAPTIVE_STATS
            .contention_activations
            .load(Ordering::Relaxed),
    }
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;
    use std::thread;

    #[test]
    fn idle_policy_keeps_the_freelist_path() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(0, 0, true, 2, 3).use_bitmap);
        assert!(!state.choose_bitmap(0, 0, true, 2, 3).use_bitmap);
    }

    #[test]
    fn new_thread_snapshots_stale_signals_without_inheriting_a_lease() {
        let mut state = AdaptiveThreadState::new();
        let decision = state.choose_bitmap(8, 3, true, 4, 3);
        assert!(!decision.use_bitmap);
        assert!(!decision.bridge_activated);
        assert!(!decision.contention_activated);
    }

    #[test]
    fn bridge_burst_starts_one_bounded_bitmap_lease() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(1, 0, true, 2, 3).use_bitmap);
        let trigger = state.choose_bitmap(3, 0, true, 2, 3);
        assert!(trigger.use_bitmap);
        assert!(trigger.bridge_activated);
        assert!(state.choose_bitmap(3, 0, false, 2, 3).use_bitmap);
        assert!(state.choose_bitmap(3, 0, false, 2, 3).use_bitmap);
        assert!(!state.choose_bitmap(3, 0, false, 2, 3).use_bitmap);
    }

    #[test]
    fn large_run_demand_refreshes_an_active_bitmap_lease() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(0, 0, true, 4, 2).use_bitmap);
        assert!(state.choose_bitmap(4, 0, true, 4, 2).use_bitmap);
        assert!(state.choose_bitmap(4, 0, true, 4, 2).use_bitmap);
        assert!(state.choose_bitmap(4, 0, true, 4, 2).use_bitmap);
        assert!(state.choose_bitmap(4, 0, false, 4, 2).use_bitmap);
        assert!(!state.choose_bitmap(4, 0, false, 4, 2).use_bitmap);
    }

    #[test]
    fn sparse_bridge_signals_do_not_accumulate_across_observations() {
        let mut state = AdaptiveThreadState::new();
        for generation in 1..=8 {
            assert!(!state.choose_bitmap(generation, 0, true, 2, 3).use_bitmap);
        }
    }

    #[test]
    fn bridge_threshold_minus_one_does_not_start_a_lease() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(0, 0, true, 4, 3).use_bitmap);
        assert!(!state.choose_bitmap(3, 0, true, 4, 3).use_bitmap);
        let decision = state.choose_bitmap(7, 0, true, 4, 3);
        assert!(decision.use_bitmap);
        assert!(decision.bridge_activated);
    }

    #[test]
    fn routing_generation_wraparound_preserves_bridge_delta() {
        let mut state = AdaptiveThreadState::new();
        assert!(
            !state
                .choose_bitmap(usize::MAX - 1, 0, true, 4, 3)
                .use_bitmap
        );
        assert!(state.choose_bitmap(2, 0, true, 4, 3).use_bitmap);
    }

    #[test]
    fn small_exact_request_consumes_a_bridge_burst_without_routing() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(8, 0, false, 4, 3).use_bitmap);
        assert!(!state.choose_bitmap(8, 0, true, 4, 3).use_bitmap);
    }

    #[test]
    fn contention_starts_and_refreshes_a_bitmap_lease() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(0, 0, false, 4, 1).use_bitmap);
        let first = state.choose_bitmap(1, 1, false, 4, 1);
        assert!(first.use_bitmap);
        assert!(first.contention_activated);
        assert!(!state.choose_bitmap(1, 1, false, 4, 1).use_bitmap);
        assert!(state.choose_bitmap(2, 2, false, 4, 1).use_bitmap);
    }

    #[test]
    fn contention_observed_after_a_fast_lease_slot_refreshes_the_lease() {
        let mut state = AdaptiveThreadState::new();
        assert!(!state.choose_bitmap(0, 0, true, 4, 2).use_bitmap);
        assert!(state.choose_bitmap(4, 0, true, 4, 2).use_bitmap);
        let deferred = state.choose_bitmap(5, 1, false, 4, 2);
        assert!(deferred.use_bitmap);
        assert!(!deferred.contention_activated);
        let refreshed = state.choose_bitmap(5, 1, false, 4, 2);
        assert!(refreshed.use_bitmap);
        assert!(refreshed.contention_activated);
        assert!(state.choose_bitmap(5, 1, false, 4, 2).use_bitmap);
        assert!(!state.choose_bitmap(5, 1, false, 4, 2).use_bitmap);
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn mixed_backend_cross_thread_frees_follow_owner_provenance() {
        let layout = Layout::from_size_align(8 * crate::PAGE_SIZE, crate::PAGE_SIZE).unwrap();
        unsafe {
            let freelist_ptr = allocate_freelist(layout);
            assert!(!freelist_ptr.is_null());
            let bitmap_ptr = HOSTED_PAGE_RUN_BITMAP
                .allocate_pooled_layout(layout)
                .unwrap();

            assert!(!HOSTED_PAGE_RUN_BITMAP
                .try_deallocate_owned(freelist_ptr, layout)
                .unwrap());

            let freelist_addr = freelist_ptr as usize;
            let bitmap_addr = bitmap_ptr as usize;
            thread::spawn(move || {
                deallocate_layout(freelist_addr as *mut u8, layout);
                deallocate_layout(bitmap_addr as *mut u8, layout);
            })
            .join()
            .unwrap();
        }
    }
}
