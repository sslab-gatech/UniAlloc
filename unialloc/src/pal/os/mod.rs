//! Platform specific features

// Restartable sequence support is a Linux x86_64 userspace optimization in this
// crate: the fast path below uses x86_64 inline assembly.  Non-Linux targets,
// Linux non-x86_64 targets, and fixed-heap builds must use the conservative
// single-CPU fallback rather than compiling or calling the Linux rseq syscall
// path.
#[cfg(rseq)]
pub mod linux_rseq;
#[cfg(rseq)]
pub use linux_rseq as rseq;

#[cfg(all(not(rseq), not(feature = "fixed_heap")))]
pub mod rseq_fallback;
#[cfg(all(not(rseq), not(feature = "fixed_heap")))]
pub use rseq_fallback as rseq;
