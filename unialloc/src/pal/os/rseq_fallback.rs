//! Conservative restartable-sequence fallback.
//!
//! rseq is Linux-specific.  Platform retargeting builds for macOS, Windows, and
//! fixed-userland smoke targets still need the allocator code that imports
//! `pal::os::rseq::*` to compile and run without attempting Linux syscall 334.
//! The fallback exposes the same small API surface and models a single stable
//! CPU id, which is safe for correctness-oriented paths that cannot use the
//! Linux per-CPU fast path.

pub fn register_current_thread() {}

pub fn unregister_current_thread() {}

#[inline]
pub fn register_current_thread_checked() {}

#[inline]
pub fn unregister_current_thread_checked() {}

#[inline]
pub fn cpu_id() -> i32 {
    0
}

#[inline]
pub fn cpu_id_start() -> i32 {
    0
}

#[inline]
pub fn is_registered() -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fallback_rseq_is_stable_single_cpu() {
        register_current_thread_checked();
        assert!(is_registered());
        assert_eq!(cpu_id(), 0);
        assert_eq!(cpu_id_start(), 0);
        unregister_current_thread_checked();
        assert!(is_registered());
    }
}
