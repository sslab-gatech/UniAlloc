#![cfg(all(not(feature = "type_isolation"), not(debug_assertions)))]

use core::alloc::Layout;
use std::panic::{catch_unwind, AssertUnwindSafe};
use unialloc::{AllocationMetadata, SemanticAlloc, UniAlloc};

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

#[cfg(not(feature = "stats"))]
mod raw_only_global_alloc {
    use super::*;
    use core::alloc::GlobalAlloc;
    use unialloc::{
        active_allocation_metadata, semantic_auto_compiler_metadata_enable,
        semantic_auto_metadata_disable, semantic_auto_metadata_enable,
        semantic_auto_metadata_enabled,
    };

    fn assert_raw_global_alloc_still_works() {
        let allocator = UniAlloc::new();
        let layout = Layout::from_size_align(64, 8).unwrap();
        let ptr = unsafe { GlobalAlloc::alloc(&allocator, layout) };
        assert!(!ptr.is_null());
        unsafe {
            GlobalAlloc::dealloc(&allocator, ptr, layout);
        }
    }

    #[test]
    fn release_global_metadata_activation_fails_before_publishing_state() {
        let layout_result = catch_unwind(AssertUnwindSafe(|| {
            semantic_auto_metadata_enable(0xC0DE, 0, 0xA110);
        }));
        let layout_mode_published = semantic_auto_metadata_enabled();
        semantic_auto_metadata_disable();

        let type_ids = [0xC002_u64];
        let compiler_result = catch_unwind(AssertUnwindSafe(|| unsafe {
            semantic_auto_compiler_metadata_enable(
                0xC0DE,
                0,
                0xA111,
                type_ids.as_ptr(),
                type_ids.len(),
            )
        }));
        let compiler_mode_published = semantic_auto_metadata_enabled();
        semantic_auto_metadata_disable();

        assert!(
            layout_result.is_err(),
            "raw-only release builds must reject layout auto-metadata activation immediately"
        );
        assert!(
            compiler_result.is_err(),
            "raw-only release builds must reject compiler auto-metadata activation immediately"
        );
        assert!(
            !layout_mode_published && !compiler_mode_published,
            "rejected metadata activation must not publish process-wide state"
        );
        assert_raw_global_alloc_still_works();
    }

    #[test]
    fn release_scoped_metadata_activation_fails_before_publishing_state() {
        let metadata = AllocationMetadata::for_type(0xC002_0001);
        let result = catch_unwind(AssertUnwindSafe(|| unsafe {
            unialloc::alloc_api::set_active_metadata(metadata)
        }));

        if result.is_ok() {
            unsafe {
                unialloc::alloc_api::restore_active_metadata(AllocationMetadata::unknown());
            }
        }

        assert!(
            result.is_err(),
            "raw-only release builds must reject scoped metadata activation immediately"
        );
        assert_eq!(
            active_allocation_metadata(),
            None,
            "rejected scoped metadata activation must leave TLS state empty"
        );
        assert_raw_global_alloc_still_works();
    }

    #[test]
    fn release_compiler_scope_push_fails_before_returning_to_the_caller() {
        const CHILD_ENV: &str = "UNIALLOC_TEST_UNSUPPORTED_SCOPE_PUSH";
        if std::env::var_os(CHILD_ENV).is_some() {
            unialloc::alloc_api::__unialloc_semantic_scope_push(
                0xC002_0002,
                0xC0DE,
                unialloc::FLAG_TYPE_ISOLATED,
                0xA112,
            );
            return;
        }

        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg(
                "raw_only_global_alloc::release_compiler_scope_push_fails_before_returning_to_the_caller",
            )
            .arg("--nocapture")
            .env(CHILD_ENV, "1")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .expect("release scope-push contract child must start");

        assert!(
            !status.success(),
            "raw-only release compiler scope activation must fail closed"
        );
        assert_raw_global_alloc_still_works();
    }
}
