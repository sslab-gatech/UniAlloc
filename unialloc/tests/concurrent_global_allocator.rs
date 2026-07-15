extern crate alloc;

use alloc::boxed::Box;
use alloc::vec::Vec;
use std::sync::{Arc, Barrier};
use std::thread;

include!("allocator.rs");

#[test]
fn concurrent_small_allocations_survive_thread_cache_cleanup() {
    const THREADS: usize = 16;
    const ITERS: usize = 768;

    let barrier = Arc::new(Barrier::new(THREADS));
    let mut handles = Vec::with_capacity(THREADS);

    for thread_id in 0..THREADS {
        let barrier = Arc::clone(&barrier);
        handles.push(thread::spawn(move || {
            barrier.wait();

            let mut checksum = 0usize;
            for iter in 0..ITERS {
                let len = 1 + ((thread_id * 97 + iter * 31) % 4096);
                let byte = (thread_id ^ iter) as u8;
                let mut data = Vec::with_capacity(len);
                data.resize(len, byte);

                checksum = checksum.wrapping_add(data[0] as usize);
                checksum = checksum.wrapping_add(data[len - 1] as usize);

                if iter % 5 == 0 {
                    let boxed =
                        Box::new([thread_id as u64, iter as u64, len as u64, checksum as u64]);
                    checksum ^= boxed[0] as usize ^ boxed[1] as usize ^ boxed[2] as usize;
                }
            }

            checksum
        }));
    }

    let mut combined = 0usize;
    for handle in handles {
        combined ^= handle.join().expect("allocator worker should not panic");
    }

    assert_ne!(combined, 0);
}

#[cfg(not(feature = "fixed_heap"))]
#[test]
fn cross_thread_single_slot_free_reuses_locally_and_survives_teardown() {
    use core::alloc::{GlobalAlloc, Layout};

    let layout = Layout::from_size_align(16 * 1024, unialloc::PAGE_SIZE)
        .expect("single-slot cross-thread layout should be valid");
    let address = thread::spawn(move || unsafe {
        let ptr = GlobalAlloc::alloc(&A, layout);
        assert!(!ptr.is_null());
        core::ptr::write_bytes(ptr, 0x5a, layout.size());
        ptr as usize
    })
    .join()
    .expect("producer should allocate the single-slot object");

    thread::spawn(move || unsafe {
        let ptr = address as *mut u8;
        assert_eq!(*ptr, 0x5a);
        assert_eq!(*ptr.add(layout.size() - 1), 0x5a);
        GlobalAlloc::dealloc(&A, ptr, layout);

        let retained = unialloc::thread_cache_footprint_snapshot();
        assert!(retained.cache_initialized);
        assert!(retained.accounting_matches_exact);
        assert!(retained.exact_cached_object_bytes >= layout.size());

        let reused = GlobalAlloc::alloc(&A, layout);
        assert_eq!(
            reused, ptr,
            "the consumer thread should reuse its hot object"
        );
        GlobalAlloc::dealloc(&A, reused, layout);
    })
    .join()
    .expect("consumer should free and reuse the cross-thread object");

    // The consumer's TLS destructor returned its retained object to the slab.
    // A fresh thread can allocate and release the same class after teardown.
    thread::spawn(move || unsafe {
        let ptr = GlobalAlloc::alloc(&A, layout);
        assert!(!ptr.is_null());
        GlobalAlloc::dealloc(&A, ptr, layout);
    })
    .join()
    .expect("post-teardown single-slot allocation should succeed");
}

#[cfg(feature = "fixed_heap")]
#[test]
fn concurrent_direct_fixed_heap_allocations_serialize_the_process_cache() {
    use core::alloc::{GlobalAlloc, Layout};

    const THREADS: usize = 12;
    const ITERS: usize = 256;
    const LIVE_BATCH: usize = 24;

    init_fixed_heap_for_direct_allocator_tests();
    let barrier = Arc::new(Barrier::new(THREADS));
    let mut handles = Vec::with_capacity(THREADS);

    for thread_id in 0..THREADS {
        let barrier = Arc::clone(&barrier);
        handles.push(thread::spawn(move || {
            barrier.wait();
            let mut checksum = 0usize;

            for batch in 0..(ITERS / LIVE_BATCH) {
                let mut live = Vec::with_capacity(LIVE_BATCH);
                for offset in 0..LIVE_BATCH {
                    let iter = batch * LIVE_BATCH + offset;
                    let size = 8 + ((thread_id * 131 + iter * 47) % 4088);
                    let align = if iter % 11 == 0 { 64 } else { 8 };
                    let layout = Layout::from_size_align(size, align).expect("valid test layout");
                    let ptr = unsafe { GlobalAlloc::alloc(&A, layout) };
                    assert!(!ptr.is_null(), "fixed-heap direct allocation failed");

                    let pattern = (thread_id as u8).wrapping_mul(17) ^ iter as u8;
                    unsafe { core::ptr::write_bytes(ptr, pattern, size) };
                    live.push((ptr, layout, pattern));

                    if iter % 7 == 0 {
                        let snapshot = unialloc::thread_cache_footprint_snapshot();
                        assert!(snapshot.cache_initialized);
                        assert!(snapshot.accounting_matches_exact);
                    }
                }

                for (ptr, layout, pattern) in live.into_iter().rev() {
                    unsafe {
                        assert_eq!(*ptr, pattern);
                        assert_eq!(*ptr.add(layout.size() - 1), pattern);
                        GlobalAlloc::dealloc(&A, ptr, layout);
                    }
                    checksum = checksum.wrapping_add(pattern as usize + layout.size());
                }
            }

            checksum
        }));
    }

    let mut combined = 0usize;
    for handle in handles {
        combined = combined.wrapping_add(handle.join().expect("allocator worker should finish"));
    }

    assert_ne!(combined, 0);
    let snapshot = unialloc::thread_cache_footprint_snapshot();
    assert!(snapshot.cache_initialized);
    assert!(snapshot.accounting_matches_exact);
}
