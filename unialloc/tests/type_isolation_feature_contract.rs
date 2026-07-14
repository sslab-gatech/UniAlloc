#[cfg(all(not(feature = "type_isolation"), not(debug_assertions)))]
use core::alloc::Layout;
#[cfg(all(not(feature = "type_isolation"), not(debug_assertions)))]
use std::panic::{catch_unwind, AssertUnwindSafe};
#[cfg(all(not(feature = "type_isolation"), not(debug_assertions)))]
use unialloc::{AllocationMetadata, SemanticAlloc, UniAlloc};

#[cfg(all(not(feature = "type_isolation"), not(debug_assertions)))]
#[test]
fn release_semantic_allocation_requires_type_isolation_feature() {
    let allocator = UniAlloc::new();
    let layout = Layout::from_size_align(64, 8).unwrap();
    let metadata = AllocationMetadata::for_type(0xA110_C001);

    let result = catch_unwind(AssertUnwindSafe(|| unsafe {
        SemanticAlloc::alloc_with_metadata(&allocator, layout, metadata)
    }));

    assert!(
        result.is_err(),
        "raw-only release builds must reject semantic allocation APIs"
    );
}
