// defining the allocator used in test
use unialloc::UniAlloc;

#[cfg_attr(not(feature = "fixed_heap"), global_allocator)]
static A: UniAlloc = UniAlloc;

#[cfg(feature = "fixed_heap")]
pub fn init_fixed_heap_for_direct_allocator_tests() {
    use core::sync::atomic::{AtomicBool, Ordering};

    const FIXED_HEAP_TEST_BYTES: usize = 256 * 1024 * 1024;

    #[repr(align(16384))]
    struct FixedHeapTestHeap([u8; FIXED_HEAP_TEST_BYTES]);

    static INITIALIZING: AtomicBool = AtomicBool::new(false);
    static READY: AtomicBool = AtomicBool::new(false);
    static mut HEAP: FixedHeapTestHeap = FixedHeapTestHeap([0; FIXED_HEAP_TEST_BYTES]);

    if READY.load(Ordering::Acquire) {
        return;
    }
    if INITIALIZING
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_ok()
    {
        unsafe {
            let heap = core::ptr::addr_of_mut!(HEAP);
            let start = core::ptr::addr_of_mut!((*heap).0).cast::<u8>() as usize;
            assert!(
                A.try_init(start, FIXED_HEAP_TEST_BYTES, unialloc::PAGE_SIZE),
                "fixed_heap direct allocator tests failed to initialize their heap range"
            );
        }
        READY.store(true, Ordering::Release);
    } else {
        while !READY.load(Ordering::Acquire) {
            core::hint::spin_loop();
        }
    }
}
