#![no_std]
#![no_main]
#![feature(alloc_error_handler)]

// `unialloc_bridge` calls only exported C ABI symbols.  Keep the dependency in
// the final crate's link exactly as rust_bench.rs must do when the kernel build
// passes UniAlloc through `--extern`.
extern crate unialloc as _;

use core::alloc::Layout;
use core::panic::PanicInfo;

mod abi_layout_contract;
#[path = "../../../kernel/kernel-modules/benchmarking/unialloc_bridge.rs"]
mod unialloc_bridge;

/// Keep representative fixed-heap and semantic snapshot bridge paths live in
/// the linked ELF.  The regression inspects this image; it does not execute it
/// as a substitute for a Rust-for-Linux kernel module runtime.
#[no_mangle]
pub extern "C" fn rust_for_linux_unialloc_link_probe() -> bool {
    if !unialloc_bridge::ensure_initialized() {
        return false;
    }

    unialloc_bridge::reset_semantic_stats();
    let stats_ok = unialloc_bridge::semantic_stats_snapshot().is_some();
    let mut rows = [unialloc_bridge::SemanticTypeStatsSnapshot::empty(); 1];
    let type_stats_ok = unialloc_bridge::semantic_type_stats_snapshot(&mut rows).is_some();
    let metadata_ok = unialloc_bridge::semantic_metadata_validation_snapshot().is_some();
    let c_abi = unialloc_bridge::run_c_abi_probe();
    stats_ok && type_stats_ok && metadata_ok && c_abi.invoked
}

#[no_mangle]
pub extern "C" fn _start() -> ! {
    let _ = rust_for_linux_unialloc_link_probe();
    halt_forever()
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
