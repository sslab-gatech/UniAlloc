// TODO: change it to private module
pub mod arch;
pub mod os;
#[cfg(not(feature = "fixed_heap"))]
pub mod sync;
#[cfg(not(feature = "fixed_heap"))]
pub mod sys_alloc;
// The production allocator is `no_std`; this std-backed thread shim is only
// used by unit tests that need to exercise cross-thread behavior.  Keeping it
// out of normal builds lets arm64e no_std runtime probes bypass Rust std
// startup while still testing the allocator/PAC path.
#[cfg(test)]
pub mod thread;
#[cfg(not(feature = "fixed_heap"))]
pub use sys_alloc::PageHeap as SystemAllocator;
#[cfg(not(feature = "fixed_heap"))]
pub use sys_alloc::{mmap, mprotect, munmap, prots};
