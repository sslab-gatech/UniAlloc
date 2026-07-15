//! Bounded allocator-edge witness for RUSTSEC-2023-0017.
//!
//! `maligned::align_first::<u8, A256>(1009)` allocates with this exact
//! `(size=1009, align=256)` layout and returns a `Vec<u8>` whose Drop glue uses
//! `(size=1009, align=1)`. The upstream harness and Miri establish that source
//! edge; this derived witness isolates the allocator's recovery-record check.

use unialloc::alloc_api::{__unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata};
use unialloc::{
    semantic_allocation_layout_validation_snapshot, semantic_auto_metadata_disable,
    semantic_stats_reset,
};

const SIZE: usize = 1009;
const ALLOCATION_ALIGN: usize = 256;
const DEALLOCATION_ALIGN: usize = 1;
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
        let ptr = __unialloc_alloc_with_metadata(
            SIZE,
            ALLOCATION_ALIGN,
            TYPE_ID,
            MODULE_ID,
            policy_flags,
            CALLSITE,
        );
        assert!(!ptr.is_null(), "RSH-031 derived allocation failed");

        // Reconstruct the vulnerable ownership shape: Vec<u8> Drop submits
        // align=1 even though the recovery record authorizes align=256.
        let values = Vec::<u8>::from_raw_parts(ptr, 0, SIZE);
        drop(values);

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
        assert_eq!(signal.recovery_deallocation_layout_mismatches, 1);
        assert_eq!(
            (signal.last_requested_size, signal.last_requested_align),
            (SIZE, DEALLOCATION_ALIGN)
        );
        assert_eq!(
            (signal.last_recorded_size, signal.last_recorded_align),
            (SIZE, ALLOCATION_ALIGN)
        );
        assert_eq!(signal.last_recorded_type_id, TYPE_ID);
        assert_eq!(signal.last_recorded_module_id, MODULE_ID);
        assert_eq!(signal.last_recorded_callsite, CALLSITE);

        // The failed deallocation preserved the authoritative record, so an
        // exact retry performs bounded cleanup without adding another signal.
        assert!(__unialloc_dealloc_with_metadata(
            ptr,
            SIZE,
            ALLOCATION_ALIGN,
            TYPE_ID,
            MODULE_ID,
            policy_flags,
            CALLSITE,
        ));
    }
}
