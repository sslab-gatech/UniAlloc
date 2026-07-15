#![cfg(all(not(feature = "type_isolation"), not(debug_assertions)))]

use core::alloc::Layout;
use std::panic::{catch_unwind, AssertUnwindSafe};
use unialloc::{AllocationMetadata, SemanticAlloc, UniAlloc};

#[cfg(feature = "fixed_heap")]
#[path = "../src/bin_support/fixed_heap_probe_global.rs"]
#[allow(dead_code)]
mod fixed_heap_probe_global;

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

#[test]
fn release_rust_scope_push_rejection_preserves_depth() {
    let before = unialloc::semantic_scope_depth_snapshot();
    let result = catch_unwind(AssertUnwindSafe(|| {
        unialloc::alloc_api::type_isolation::__unialloc_semantic_scope_push_for_rust_type::<u64>(
            0xC0DE,
            unialloc::FLAG_TYPE_ISOLATED,
            0xA116,
        );
    }));

    assert!(
        result.is_err(),
        "release builds must reject Rust semantic scope activation without type isolation"
    );
    assert_eq!(
        unialloc::semantic_scope_depth_snapshot(),
        before,
        "rejected Rust scope activation must not mutate TLS depth"
    );
}

#[cfg(not(any(feature = "stats", feature = "reclaim_checks")))]
mod raw_only_global_alloc {
    use super::*;
    use core::alloc::GlobalAlloc;
    use unialloc::{
        active_allocation_metadata, semantic_auto_compiler_metadata_enable,
        semantic_auto_metadata_disable, semantic_auto_metadata_enable,
        semantic_auto_metadata_enabled,
    };

    fn ensure_allocator_ready() {
        #[cfg(feature = "fixed_heap")]
        super::fixed_heap_probe_global::ensure_initialized_for_probe();
    }

    fn assert_raw_global_alloc_still_works() {
        ensure_allocator_ready();
        let allocator = UniAlloc::new();
        let layout = Layout::from_size_align(64, 8).unwrap();
        let ptr = unsafe { GlobalAlloc::alloc(&allocator, layout) };
        assert!(!ptr.is_null());
        unsafe {
            GlobalAlloc::dealloc(&allocator, ptr, layout);
        }
    }

    #[test]
    fn release_global_realloc_preserves_payload_within_and_across_size_classes() {
        ensure_allocator_ready();
        let allocator = UniAlloc::new();
        let old_layout = Layout::from_size_align(25, 8).unwrap();
        let same_class_layout = Layout::from_size_align(32, old_layout.align()).unwrap();
        let cross_class_layout = Layout::from_size_align(513, old_layout.align()).unwrap();
        let mut ptr = unsafe { GlobalAlloc::alloc(&allocator, old_layout) };
        assert!(!ptr.is_null());
        for offset in 0..old_layout.size() {
            unsafe {
                ptr.add(offset).write((offset as u8) ^ 0xA5);
            }
        }

        let same_class_ptr =
            unsafe { GlobalAlloc::realloc(&allocator, ptr, old_layout, same_class_layout.size()) };
        assert_eq!(same_class_ptr, ptr, "same-class growth must stay in place");
        for offset in 0..old_layout.size() {
            assert_eq!(
                unsafe { same_class_ptr.add(offset).read() },
                (offset as u8) ^ 0xA5
            );
        }
        ptr = same_class_ptr;

        let cross_class_ptr = unsafe {
            GlobalAlloc::realloc(
                &allocator,
                ptr,
                same_class_layout,
                cross_class_layout.size(),
            )
        };
        assert!(!cross_class_ptr.is_null());
        for offset in 0..old_layout.size() {
            assert_eq!(
                unsafe { cross_class_ptr.add(offset).read() },
                (offset as u8) ^ 0xA5
            );
        }
        unsafe {
            GlobalAlloc::dealloc(&allocator, cross_class_ptr, cross_class_layout);
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
        for metadata in [
            AllocationMetadata::for_type(0xC002_0001).with_flags(0),
            AllocationMetadata::for_type(0xC002_0002),
        ] {
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
                "raw-only release builds must reject flags-zero and policy-bearing scoped metadata immediately"
            );
            assert_eq!(
                active_allocation_metadata(),
                None,
                "rejected scoped metadata activation must leave TLS state empty"
            );
        }
        assert_raw_global_alloc_still_works();
    }

    #[test]
    fn release_ffi_activation_aborts_fail_closed() {
        const CHILD_ENV: &str = "UNIALLOC_TEST_UNSUPPORTED_FFI_ACTIVATION";
        const TEST_NAME: &str = "raw_only_global_alloc::release_ffi_activation_aborts_fail_closed";

        if let Ok(case) = std::env::var(CHILD_ENV) {
            match case.as_str() {
                "layout" => {
                    unialloc::alloc_api::__unialloc_semantic_auto_metadata_enable(
                        0xC0DE, 0, 0xA112,
                    );
                }
                "compiler" => {
                    let type_ids = [0xC002_0002];
                    unsafe {
                        unialloc::alloc_api::__unialloc_semantic_auto_compiler_metadata_enable(
                            0xC0DE,
                            0,
                            0xA113,
                            type_ids.as_ptr(),
                            type_ids.len(),
                        );
                    }
                }
                "stream" => {
                    let type_ids = [0xC002_0003];
                    unsafe {
                        unialloc::alloc_api::__unialloc_semantic_auto_compiler_metadata_stream_enable(
                            0xC0DE,
                            0,
                            0xA114,
                            type_ids.as_ptr(),
                            type_ids.len(),
                        );
                    }
                }
                "scope" => {
                    unialloc::alloc_api::__unialloc_semantic_scope_push(
                        0xC002_0004,
                        0xC0DE,
                        unialloc::FLAG_TYPE_ISOLATED,
                        0xA115,
                    );
                }
                "scope_transport" => {
                    unialloc::alloc_api::__unialloc_semantic_scope_push(
                        0xC002_0005,
                        0xC0DE,
                        0,
                        0xA116,
                    );
                }
                _ => panic!("unknown FFI activation child case: {}", case),
            }
            return;
        }

        for case in ["layout", "compiler", "stream", "scope", "scope_transport"] {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .arg("--exact")
                .arg(TEST_NAME)
                .arg("--nocapture")
                .env(CHILD_ENV, case)
                .stdout(std::process::Stdio::null())
                .output()
                .expect("release FFI activation contract child must start");

            assert!(
                !output.status.success(),
                "raw-only release FFI {} activation must abort fail-closed",
                case
            );
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(
                stderr.contains("semantic allocation APIs require the type_isolation feature"),
                "raw-only release FFI {} activation failed for an unexpected reason: {}",
                case,
                stderr
            );
        }
        assert_raw_global_alloc_still_works();
    }
}

#[cfg(feature = "reclaim_checks")]
mod reclaim_checks_global_alloc {
    use super::*;
    use core::alloc::GlobalAlloc;

    fn ensure_allocator_ready() {
        #[cfg(feature = "fixed_heap")]
        super::fixed_heap_probe_global::ensure_initialized_for_probe();
    }

    #[test]
    fn release_reclaim_checks_preserve_legal_global_realloc() {
        ensure_allocator_ready();
        let allocator = UniAlloc::new();
        let old_layout = Layout::from_size_align(64, 8).unwrap();
        let new_layout = Layout::from_size_align(513, old_layout.align()).unwrap();
        let ptr = unsafe { GlobalAlloc::alloc(&allocator, old_layout) };
        assert!(!ptr.is_null());
        for offset in 0..old_layout.size() {
            unsafe {
                ptr.add(offset).write((offset as u8).wrapping_mul(17));
            }
        }

        let grown = unsafe { GlobalAlloc::realloc(&allocator, ptr, old_layout, new_layout.size()) };
        assert!(!grown.is_null());
        for offset in 0..old_layout.size() {
            assert_eq!(
                unsafe { grown.add(offset).read() },
                (offset as u8).wrapping_mul(17)
            );
        }
        unsafe {
            GlobalAlloc::dealloc(&allocator, grown, new_layout);
        }
    }
}
