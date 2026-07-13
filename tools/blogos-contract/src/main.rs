#![no_std]
#![no_main]
#![feature(alloc_error_handler)]

extern crate alloc;

use alloc::boxed::Box;
use core::alloc::{GlobalAlloc, Layout};
use core::panic::PanicInfo;
use unialloc::UniAlloc;

mod boot_init_state;

use boot_init_state::BootInitState;

// This is a linkable contract fixture, not a replacement for the heap mapping
// performed by the real BlogOS boot path.  The aligned BSS range lets the
// fixture connect boot initialization to a real allocation without requiring
// an external bootloader image or QEMU runtime.
const CONTRACT_HEAP_BYTES: usize = 64 * 1024 * 1024;

#[repr(align(4096))]
struct ContractHeap([u8; CONTRACT_HEAP_BYTES]);

static BOOT_INIT: BootInitState = BootInitState::new();
static INNER: UniAlloc = UniAlloc::new();
static mut CONTRACT_HEAP: ContractHeap = ContractHeap([0; CONTRACT_HEAP_BYTES]);

struct BlogOsGlobalAllocator;

#[global_allocator]
static GLOBAL_ALLOCATOR: BlogOsGlobalAllocator = BlogOsGlobalAllocator;

#[inline]
fn allocator_ready() -> bool {
    BOOT_INIT.ready()
}

unsafe impl GlobalAlloc for BlogOsGlobalAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if allocator_ready() {
            GlobalAlloc::alloc(&INNER, layout)
        } else {
            core::ptr::null_mut()
        }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        if allocator_ready() {
            GlobalAlloc::dealloc(&INNER, ptr, layout);
        }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        if allocator_ready() {
            GlobalAlloc::alloc_zeroed(&INNER, layout)
        } else {
            core::ptr::null_mut()
        }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if allocator_ready() {
            GlobalAlloc::realloc(&INNER, ptr, layout, new_size)
        } else {
            core::ptr::null_mut()
        }
    }
}

/// Publish a bootloader-mapped heap range to UniAlloc exactly once.
///
/// A BlogOS kernel should call this after its mapper has made the full range
/// writable and before any `alloc` collection is constructed.  Concurrent
/// callers wait for the initial publisher rather than resetting live allocator
/// roots.
///
/// # Safety
///
/// `heap_start..heap_start + heap_size` must remain exclusively owned by
/// UniAlloc for the rest of the boot and must not overlap kernel objects.  No
/// other UniAlloc initialization API may run before or concurrently with this
/// wrapper; this wrapper must be the sole owner of fixed-heap publication.
#[no_mangle]
pub unsafe extern "C" fn blogos_unialloc_boot_init(heap_start: usize, heap_size: usize) -> bool {
    BOOT_INIT.publish(heap_start, heap_size, || {
        // `UniAlloc::try_init` treats an already-ready fixed heap as a
        // successful no-op.  Reject that state here rather than falsely
        // publishing this caller's unrelated range as the active heap.  The
        // safety contract above excludes the remaining cross-API race between
        // this check and `try_init`.
        if unialloc::fixed_heap_ready() {
            return false;
        }

        INNER.try_init(heap_start, heap_size, unialloc::PAGE_SIZE)
    })
}

/// Exercise the boot-to-global-allocation handoff used by the build contract.
#[no_mangle]
pub unsafe extern "C" fn blogos_unialloc_contract_probe(
    heap_start: usize,
    heap_size: usize,
) -> bool {
    if !blogos_unialloc_boot_init(heap_start, heap_size) {
        return false;
    }

    let value = Box::new(0xB10B_0500_D15C_A11C_u64);
    let passed = *value == 0xB10B_0500_D15C_A11C_u64 && unialloc::fixed_heap_ready();
    drop(value);
    passed
}

#[no_mangle]
pub extern "C" fn _start() -> ! {
    let heap = core::ptr::addr_of_mut!(CONTRACT_HEAP);
    let heap_start = unsafe { core::ptr::addr_of_mut!((*heap).0).cast::<u8>() as usize };
    let passed = unsafe { blogos_unialloc_contract_probe(heap_start, CONTRACT_HEAP_BYTES) };
    if passed {
        halt_forever()
    }
    panic!("BlogOS UniAlloc boot contract failed")
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    halt_forever()
}

#[alloc_error_handler]
fn alloc_error(_layout: Layout) -> ! {
    halt_forever()
}

#[inline(never)]
fn halt_forever() -> ! {
    loop {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            core::arch::asm!("cli", "hlt", options(nomem, nostack));
        }
        #[cfg(not(target_arch = "x86_64"))]
        core::hint::spin_loop();
    }
}
