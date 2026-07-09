use core::alloc::{GlobalAlloc, Layout};
use core::sync::atomic::{AtomicU8, Ordering};

use unialloc::UniAlloc;

const INIT_UNINITIALIZED: u8 = 0;
const INIT_IN_PROGRESS: u8 = 1;
const INIT_READY: u8 = 2;
const FIXED_HEAP_PROBE_BYTES: usize = 256 * 1024 * 1024;

#[repr(align(16384))]
struct FixedHeapProbeHeap([u8; FIXED_HEAP_PROBE_BYTES]);

static INIT_STATE: AtomicU8 = AtomicU8::new(INIT_UNINITIALIZED);
static mut FIXED_HEAP_PROBE_HEAP: FixedHeapProbeHeap =
    FixedHeapProbeHeap([0; FIXED_HEAP_PROBE_BYTES]);

pub struct FixedHeapProbeAllocator;

#[inline]
pub fn ensure_initialized_for_probe() {
    if INIT_STATE.load(Ordering::Acquire) == INIT_READY {
        return;
    }

    if INIT_STATE
        .compare_exchange(
            INIT_UNINITIALIZED,
            INIT_IN_PROGRESS,
            Ordering::AcqRel,
            Ordering::Acquire,
        )
        .is_ok()
    {
        unsafe {
            // Avoid forming a `&mut` to the global probe heap; the init state
            // above grants one-time initialization and this raw pointer is only
            // handed to UniAlloc as a fixed backing range.
            let heap = core::ptr::addr_of_mut!(FIXED_HEAP_PROBE_HEAP);
            let start = core::ptr::addr_of_mut!((*heap).0).cast::<u8>() as usize;
            UniAlloc.init(start, FIXED_HEAP_PROBE_BYTES, unialloc::PAGE_SIZE);
        }
        INIT_STATE.store(INIT_READY, Ordering::Release);
        return;
    }

    while INIT_STATE.load(Ordering::Acquire) != INIT_READY {
        core::hint::spin_loop();
    }
}

unsafe impl GlobalAlloc for FixedHeapProbeAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        ensure_initialized_for_probe();
        UniAlloc.alloc(layout)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        ensure_initialized_for_probe();
        UniAlloc.dealloc(ptr, layout);
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        ensure_initialized_for_probe();
        UniAlloc.alloc_zeroed(layout)
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        ensure_initialized_for_probe();
        UniAlloc.realloc(ptr, layout, new_size)
    }
}
