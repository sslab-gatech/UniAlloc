//! Pure initialization-state transitions for the Rust-for-Linux UniAlloc bridge.
//!
//! Keeping this logic independent from the kernel and allocator FFI makes the
//! retry/ownership contract directly testable with `rustc --test`.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BridgeInitializationAction {
    Initialize,
    Extend(usize),
    MarkReady,
    Reject,
}

pub(crate) const fn bridge_initialization_action(
    committed_bytes: usize,
    heap_capacity: usize,
    allocator_ready: bool,
) -> BridgeInitializationAction {
    if committed_bytes > heap_capacity {
        return BridgeInitializationAction::Reject;
    }
    if committed_bytes == 0 {
        return if allocator_ready {
            // A globally ready allocator with no bridge-owned bytes may have
            // been initialized by another owner.  Never claim its heap.
            BridgeInitializationAction::Reject
        } else {
            BridgeInitializationAction::Initialize
        };
    }
    if !allocator_ready {
        return BridgeInitializationAction::Reject;
    }
    if committed_bytes == heap_capacity {
        BridgeInitializationAction::MarkReady
    } else {
        BridgeInitializationAction::Extend(heap_capacity - committed_bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::{bridge_initialization_action, BridgeInitializationAction};

    const CAPACITY: usize = 8 * 1024 * 1024;
    const INITIAL: usize = CAPACITY / 2;

    #[test]
    fn rejects_a_ready_allocator_not_owned_by_the_bridge() {
        assert_eq!(
            bridge_initialization_action(0, CAPACITY, true),
            BridgeInitializationAction::Reject
        );
    }

    #[test]
    fn retries_only_the_remaining_extension_after_partial_initialization() {
        assert_eq!(
            bridge_initialization_action(INITIAL, CAPACITY, true),
            BridgeInitializationAction::Extend(CAPACITY - INITIAL)
        );
    }

    #[test]
    fn marks_ready_only_after_the_full_bridge_heap_is_committed() {
        assert_eq!(
            bridge_initialization_action(CAPACITY, CAPACITY, true),
            BridgeInitializationAction::MarkReady
        );
        assert_eq!(
            bridge_initialization_action(INITIAL, CAPACITY, false),
            BridgeInitializationAction::Reject
        );
    }
}
