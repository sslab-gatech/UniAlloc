use core::sync::atomic::{AtomicU8, AtomicUsize, Ordering};

const INIT_UNINITIALIZED: u8 = 0;
const INIT_IN_PROGRESS: u8 = 1;
const INIT_READY: u8 = 2;

/// Coordinates one-time publication of the heap range owned by UniAlloc.
///
/// The initializer runs only for the caller that wins the transition from
/// uninitialized to in-progress.  Other callers wait for that attempt to
/// finish, then accept only the exact range published by the winner.  A failed
/// attempt returns the state to uninitialized so a later caller can retry.
pub(crate) struct BootInitState {
    state: AtomicU8,
    heap_start: AtomicUsize,
    heap_size: AtomicUsize,
    #[cfg(test)]
    wait_observations: AtomicUsize,
}

impl BootInitState {
    pub(crate) const fn new() -> Self {
        Self {
            state: AtomicU8::new(INIT_UNINITIALIZED),
            heap_start: AtomicUsize::new(0),
            heap_size: AtomicUsize::new(0),
            #[cfg(test)]
            wait_observations: AtomicUsize::new(0),
        }
    }

    #[inline]
    pub(crate) fn ready(&self) -> bool {
        self.state.load(Ordering::Acquire) == INIT_READY
    }

    #[inline]
    fn published_range_matches(&self, heap_start: usize, heap_size: usize) -> bool {
        self.heap_start.load(Ordering::Relaxed) == heap_start
            && self.heap_size.load(Ordering::Relaxed) == heap_size
    }

    pub(crate) fn publish(
        &self,
        heap_start: usize,
        heap_size: usize,
        initialize: impl FnOnce() -> bool,
    ) -> bool {
        let mut initialize = Some(initialize);
        #[cfg(test)]
        let mut observed_wait = false;

        loop {
            match self.state.load(Ordering::Acquire) {
                INIT_READY => return self.published_range_matches(heap_start, heap_size),
                INIT_IN_PROGRESS => {
                    #[cfg(test)]
                    if !observed_wait {
                        self.wait_observations.fetch_add(1, Ordering::Relaxed);
                        observed_wait = true;
                    }
                    core::hint::spin_loop();
                }
                INIT_UNINITIALIZED => {
                    if self
                        .state
                        .compare_exchange(
                            INIT_UNINITIALIZED,
                            INIT_IN_PROGRESS,
                            Ordering::AcqRel,
                            Ordering::Acquire,
                        )
                        .is_err()
                    {
                        continue;
                    }

                    let initializer = initialize
                        .take()
                        .expect("initializer is consumed only by the winning caller");
                    let initialized = initializer();
                    if initialized {
                        // The Release publication of INIT_READY makes both
                        // relaxed range stores visible to Acquire readers.
                        self.heap_start.store(heap_start, Ordering::Relaxed);
                        self.heap_size.store(heap_size, Ordering::Relaxed);
                    }
                    self.state.store(
                        if initialized {
                            INIT_READY
                        } else {
                            INIT_UNINITIALIZED
                        },
                        Ordering::Release,
                    );
                    return initialized;
                }
                _ => return false,
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn wait_observations(&self) -> usize {
        self.wait_observations.load(Ordering::Acquire)
    }
}
