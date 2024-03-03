// TODO: change it to private module
pub mod arch;
pub mod os;
#[cfg(not(feature = "fixed_heap"))]
pub mod sync;
#[cfg(not(feature = "fixed_heap"))]
pub mod sys_alloc;
pub mod thread;
#[cfg(not(feature = "fixed_heap"))]
pub use sys_alloc::PageHeap as SystemAllocator;
#[cfg(not(feature = "fixed_heap"))]
pub use sys_alloc::{mmap, munmap, prots};
