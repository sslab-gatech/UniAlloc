//! Platform specific features

// restartable sequence system call
// (linux userspace only)
#[cfg(not(feature = "fixed_heap"))]
pub mod linux_rseq;
#[cfg(not(feature = "fixed_heap"))]
pub use linux_rseq as rseq;
