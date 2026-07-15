//! Exact-layout control for the RSH-031 allocator-edge witness.

use unialloc::alloc_api::{__unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata};
use unialloc::{
    semantic_allocation_layout_validation_snapshot, semantic_auto_metadata_disable,
    semantic_stats_reset,
};

const SIZE: usize = 1009;
const ALIGN: usize = 256;
const TYPE_ID: u64 = 0x5253_4830_3331_0001;
const MODULE_ID: u64 = 0x5253_4830_3331_0002;
const CALLSITE: u64 = 0x5253_4830_3331_0003;

fn main() {
    let policy_flags = std::env::var("UNIALLOC_RSH031_POLICY_FLAGS")
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or(0);
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    unsafe {
        let ptr =
            __unialloc_alloc_with_metadata(SIZE, ALIGN, TYPE_ID, MODULE_ID, policy_flags, CALLSITE);
        assert!(!ptr.is_null(), "RSH-031 patched-control allocation failed");
        assert!(__unialloc_dealloc_with_metadata(
            ptr,
            SIZE,
            ALIGN,
            TYPE_ID,
            MODULE_ID,
            policy_flags,
            CALLSITE,
        ));
    }

    let signal = semantic_allocation_layout_validation_snapshot();
    eprintln!(
        "UNIALLOC_RSH031_LAYOUT_SIGNAL={{\"policy_flags\":{},\"recovery_deallocation_layout_mismatches\":{},\"last_requested_size\":{},\"last_requested_align\":{},\"last_recorded_size\":{},\"last_recorded_align\":{},\"last_recorded_type_id\":{},\"last_recorded_module_id\":{},\"last_recorded_callsite\":{}}}",
        policy_flags,
        signal.recovery_deallocation_layout_mismatches,
        signal.last_requested_size,
        signal.last_requested_align,
        signal.last_recorded_size,
        signal.last_recorded_align,
        signal.last_recorded_type_id,
        signal.last_recorded_module_id,
        signal.last_recorded_callsite,
    );
    assert_eq!(signal.recovery_deallocation_layout_mismatches, 0);
}
